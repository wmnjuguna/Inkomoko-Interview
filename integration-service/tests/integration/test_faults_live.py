"""Part J against the live stack: Fineract behind the fault proxy, the real Mock ERP and
its fault switch. The app runs in-process with the outbox sender off, so each test sends
the outbox and runs recovery itself, and can look at the state in between.

Every fault switch is turned off before and after each test (the ERP switch is shared with
the other test runs, so the fault windows are short). Fresh fake references every test."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine, create_engine, func, select, text

from integration_service import faults
from integration_service.app import create_app
from integration_service.clients.erp import ErpClient
from integration_service.clients.errors import FineractTimeout, FineractUnavailable
from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.mapping.journal import load_gl_role_accounts
from integration_service.models import outbox
from integration_service.services.loans import fineract_from_settings
from integration_service.services.operations import get_operation
from integration_service.services.outbox import send_due
from integration_service.services.recovery import recover_once

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GL_FILE = os.path.join(os.path.dirname(ROOT), "fineract/configuration/loan-product.json")
PROXY_URL = os.environ.get("FAULT_PROXY_URL", "http://localhost:8081")
PROXY_ADMIN = os.environ.get("FAULT_PROXY_ADMIN_URL", "http://localhost:8092")
PROXY_KEY = os.environ.get("FAULT_PROXY_API_KEY", "change-me-fault-proxy-key")
FINERACT_DIRECT = "http://localhost:8080/fineract-provider/api/v1"
JOURNAL = "/api/journal-entries"
LATER = timedelta(hours=1)


def accounts() -> Any:
    return load_gl_role_accounts(GL_FILE)


def tag() -> str:
    return f"{secrets.token_hex(3)}-{secrets.randbelow(10**6)}"


# --- fault switches ---------------------------------------------------------------------------


def proxy_fault(mode: str | None, **extra: Any) -> dict[str, Any]:
    headers = {"X-API-Key": PROXY_KEY}
    url = f"{PROXY_ADMIN}/admin/faults"
    if mode is None:
        response = httpx.delete(url, headers=headers, timeout=5)
    else:
        response = httpx.put(url, json={"mode": mode, **extra}, headers=headers, timeout=5)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


def erp_fault(settings: Settings, mode: str | None, **extra: Any) -> None:
    headers = {"X-API-Key": settings.erp_api_key}
    url = f"{settings.erp_base_url}/admin/faults"
    if mode is None:
        httpx.delete(url, headers=headers, timeout=5).raise_for_status()
    else:
        body = {"mode": mode, "path_prefix": JOURNAL, **extra}
        httpx.put(url, json=body, headers=headers, timeout=5).raise_for_status()


@pytest.fixture(autouse=True)
def _faults_off(settings: Settings) -> Iterator[None]:
    proxy_fault(None)
    erp_fault(settings, None)
    try:
        yield
    finally:
        proxy_fault(None)
        erp_fault(settings, None)


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def settings(settings: Settings) -> Settings:
    password = os.environ.get("FINERACT_API_PASSWORD", "")
    if not password:
        pytest.skip("FINERACT_API_PASSWORD is not set (the API user's password from .env)")
    return replace(
        settings,
        fineract_base_url=f"{PROXY_URL}/fineract-provider/api/v1",
        fineract_username=os.environ.get("FINERACT_API_USERNAME", "integration-api"),
        fineract_password=password,
        gl_roles_config_path=GL_FILE,
        outbox_enabled=False,
    )


@pytest.fixture(autouse=True)
def _clean(db: Engine) -> None:
    start = 100000 + secrets.randbelow(890000)
    with db.begin() as conn:
        conn.execute(text(f"ALTER SEQUENCE loan_reference_seq RESTART WITH {start}"))


@pytest.fixture
def via_proxy(settings: Settings) -> Iterator[FineractClient]:
    client = fineract_from_settings(settings)
    yield client
    client.close()


@pytest.fixture
def direct() -> Iterator[FineractClient]:
    """Fineract on its real port, as the admin, only to count what it holds."""
    client = FineractClient(
        FINERACT_DIRECT,
        "default",
        os.environ.get("FINERACT_USERNAME", "mifos"),
        os.environ.get("FINERACT_PASSWORD", "password"),
        30.0,
    )
    yield client
    client.close()


@pytest.fixture
def erp(settings: Settings) -> Iterator[ErpClient]:
    client = ErpClient(settings.erp_base_url, settings.erp_api_key, timeout_seconds=10.0)
    yield client
    client.close()


def new_loan(client: FlaskClient, auth: dict[str, str]) -> str:
    uid = f"FLT-{secrets.randbelow(10**6):06d}"
    made = client.post(
        "/api/clients",
        json={
            "external_reference": f"IT-FCLI-{tag()}",
            "client_uid": uid,
            "first_name": "Fiona",
            "last_name": "Faulttest",
        },
        headers=auth,
    )
    assert made.status_code == 201, made.get_json()
    applied = client.post(
        "/api/loans",
        json={"external_reference": f"IT-FAPP-{tag()}", "client_uid": uid, "principal": 300000},
        headers=auth,
    )
    assert applied.status_code == 201, applied.get_json()
    loan = str(applied.get_json()["loan_reference"])
    approved = client.post(
        f"/api/loans/{loan}/approve", json={"external_reference": f"IT-FAPR-{tag()}"}, headers=auth
    )
    assert approved.status_code == 200, approved.get_json()
    return loan


def disburse(client: FlaskClient, auth: dict[str, str], loan: str, ref: str) -> Any:
    return client.post(
        f"/api/loans/{loan}/disburse", json={"external_reference": ref}, headers=auth
    )


def disbursements(direct: FineractClient, loan: str) -> int:
    data = direct.get(f"/loans/external-id/{loan}", params={"associations": "transactions"})
    return sum(
        1
        for t in data.get("transactions") or []
        if t["type"]["code"] == "loanTransactionType.disbursement" and not t.get("manuallyReversed")
    )


ERP_DB = os.environ.get(
    "ERP_TEST_DATABASE_URL", "postgresql+psycopg://erp:change-me-erp@localhost:5432/erp_db"
)


@pytest.fixture(scope="module")
def erp_db() -> Iterator[Engine]:
    """The ERP's own table, read only: the fault switch also fails the ERP's GETs."""
    eng = create_engine(ERP_DB)
    yield eng
    eng.dispose()


def erp_count(erp_db: Engine, op_key: str) -> int:
    with erp_db.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM journal_entries WHERE external_reference = :r"),
                {"r": op_key},
            ).scalar_one()
        )


def outbox_rows(db: Engine, op_key: str) -> list[dict[str, Any]]:
    op = get_operation(db, "DSB", op_key[4:])
    assert op is not None
    with db.connect() as conn:
        rows = conn.execute(select(outbox).where(outbox.c.operation_id == op.id)).mappings()
        return [dict(r) for r in rows]


def status(db: Engine, ref: str) -> str:
    op = get_operation(db, "DSB", ref)
    assert op is not None
    return op.status


def later() -> datetime:
    return datetime.now(UTC) + LATER


# --- the proxy itself -------------------------------------------------------------------------


def test_proxy_admin_needs_its_key() -> None:
    response = httpx.put(f"{PROXY_ADMIN}/admin/faults", json={"mode": "down"}, timeout=5)
    assert response.status_code == 401
    assert proxy_fault(None)["mode"] == "off"


def test_proxy_down_refuses_connections_then_comes_back(via_proxy: FineractClient) -> None:
    assert via_proxy.health()
    proxy_fault("down")
    # Inside the compose network the connection is refused (FineractUnavailable). From the
    # host, Docker's port forwarder accepts it first and then resets it (FineractTimeout).
    with pytest.raises((FineractUnavailable, FineractTimeout)):
        via_proxy.get("/offices")
    proxy_fault(None)
    assert via_proxy.health()


# --- J-1 to J-5 ------------------------------------------------------------------------------


def test_j1_erp_down_after_fineract_success(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    settings: Settings,
    direct: FineractClient,
    erp: ErpClient,
    erp_db: Engine,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-J1-{tag()}"
    erp_fault(settings, "down")

    answer = disburse(client, auth, loan, ref)
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json()["status"] == "ERP_PENDING"
    first = send_due(db, erp)
    assert first.retried == 1
    assert status(db, ref) == "ERP_PENDING"
    assert erp_count(erp_db, f"DSB:{ref}") == 0

    erp_fault(settings, None)
    assert send_due(db, erp, later()).sent == 1
    assert send_due(db, erp, later()).claimed == 0
    assert status(db, ref) == "COMPLETED"
    assert erp_count(erp_db, f"DSB:{ref}") == 1
    assert disbursements(direct, loan) == 1
    assert outbox_rows(db, f"DSB:{ref}")[0]["attempts"] == 2


def test_j2_fineract_down_is_not_sent_and_safe_to_retry(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    direct: FineractClient,
    erp: ErpClient,
    erp_db: Engine,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-J2-{tag()}"
    proxy_fault("down")

    refused = disburse(client, auth, loan, ref)
    assert refused.status_code == 503, refused.get_json()
    assert refused.get_json()["error"]["code"] == "FINERACT_UNAVAILABLE"
    assert status(db, ref) == "FAILED"
    assert disbursements(direct, loan) == 0
    assert outbox_rows(db, f"DSB:{ref}") == []

    proxy_fault(None)
    again = disburse(client, auth, loan, ref)
    assert again.status_code == 200, again.get_json()
    assert send_due(db, erp).sent == 1
    assert disbursements(direct, loan) == 1
    assert erp_count(erp_db, f"DSB:{ref}") == 1
    op = get_operation(db, "DSB", ref)
    assert op is not None and op.status == "COMPLETED" and op.attempts == 2


def test_j3_fineract_timeout_after_send_is_found_by_lookup_never_resent(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    settings: Settings,
    via_proxy: FineractClient,
    direct: FineractClient,
    erp: ErpClient,
    erp_db: Engine,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-J3-{tag()}"
    impatient = create_app(replace(settings, fineract_timeout_seconds=2.0), engine=db)
    partner = impatient.test_client()
    proxy_fault("timeout_after_send", delay_seconds=3)

    unclear = disburse(partner, auth, loan, ref)
    assert unclear.status_code == 504, unclear.get_json()
    assert unclear.get_json()["error"]["code"] == "FINERACT_TIMEOUT"
    shown = proxy_fault(None)
    assert shown["dropped_answers"] >= 1
    assert status(db, ref) == "FINERACT_PENDING"
    assert disbursements(direct, loan) == 1  # it did happen in Fineract
    assert outbox_rows(db, f"DSB:{ref}") == []
    assert disburse(partner, auth, loan, ref).status_code == 202

    report = recover_once(db, via_proxy, erp, accounts=accounts, now=later())
    assert report.operations.completed == 1
    assert report.outbox.sent == 1
    assert status(db, ref) == "COMPLETED"
    assert disbursements(direct, loan) == 1  # no second write
    assert erp_count(erp_db, f"DSB:{ref}") == 1
    replay = disburse(partner, auth, loan, ref)
    assert replay.status_code == 200
    assert replay.get_json()["external_reference"] == ref


def test_j4_erp_timeout_after_write_resend_is_idempotent(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    settings: Settings,
    direct: FineractClient,
    erp: ErpClient,
    erp_db: Engine,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-J4-{tag()}"
    answer = disburse(client, auth, loan, ref)
    assert answer.status_code == 200, answer.get_json()

    erp_fault(settings, "timeout_after_write", delay_seconds=3, remaining=1)
    impatient = ErpClient(settings.erp_base_url, settings.erp_api_key, timeout_seconds=1.5)
    try:
        assert send_due(db, impatient).retried == 1
    finally:
        impatient.close()
    assert status(db, ref) == "ERP_PENDING"
    assert erp_count(erp_db, f"DSB:{ref}") == 1  # saved, though we got no answer

    assert send_due(db, erp, later()).sent == 1
    assert status(db, ref) == "COMPLETED"
    assert erp_count(erp_db, f"DSB:{ref}") == 1
    assert outbox_rows(db, f"DSB:{ref}")[0]["attempts"] == 2
    assert disbursements(direct, loan) == 1


class Crashed(BaseException):
    """Stands in for os._exit: nothing after it runs."""


def test_j5_crash_after_fineract_done_is_sent_once_after_restart(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    settings: Settings,
    via_proxy: FineractClient,
    direct: FineractClient,
    erp: ErpClient,
    erp_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-J5-{tag()}"
    seen: list[str] = []

    def die() -> None:
        op = get_operation(db, "DSB", ref)
        assert op is not None
        seen.append(op.status)  # what is committed at the moment of the crash
        raise Crashed

    monkeypatch.setenv(faults.CRASH_ENV, "IT-J5-")
    monkeypatch.setattr(faults, "_die", die)
    with pytest.raises(Crashed):
        disburse(client, auth, loan, ref)
    monkeypatch.delenv(faults.CRASH_ENV)

    assert seen == ["ERP_PENDING"]  # FINERACT_DONE and the outbox row were committed
    rows = outbox_rows(db, f"DSB:{ref}")
    assert [r["status"] for r in rows] == ["PENDING"]
    assert rows[0]["attempts"] == 0
    assert erp_count(erp_db, f"DSB:{ref}") == 0
    assert disbursements(direct, loan) == 1

    # The restart: a new app, and its first background tick is the start-up sweep.
    restarted = create_app(settings, engine=db)
    report = recover_once(db, via_proxy, erp, accounts=accounts)
    assert report.outbox.sent == 1
    assert recover_once(db, via_proxy, erp, accounts=accounts, now=later()).outbox.claimed == 0
    assert status(db, ref) == "COMPLETED"
    assert erp_count(erp_db, f"DSB:{ref}") == 1
    assert disbursements(direct, loan) == 1

    retry = disburse(restarted.test_client(), auth, loan, ref)
    assert retry.status_code == 200
    op = get_operation(db, "DSB", ref)
    assert op is not None and retry.get_json() == op.response_body
    with db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(outbox)).scalar_one() >= 1
