"""GL sync against the running Fineract and Mock ERP, through the service and the API.

Fineract is shared and keeps what earlier runs synced, so these tests check the end state
(every ERP account SYNCED, a second run writes nothing), not first-run outcomes. Nothing is
deleted. A test that must create a new GL account uses a fresh 8-digit code starting with 7."""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine, func, select, text

from integration_service.app import create_app
from integration_service.clients.erp import ErpAccount, ErpClient
from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.mapping.account_types import build_fineract_gl_body
from integration_service.models import gl_sync
from integration_service.services.gl_sync import GL_SYNC_LOCK_KEY, sync_all

UNREACHABLE = "http://127.0.0.1:9"


def fineract_client(settings: Settings, base_url: str | None = None) -> FineractClient:
    return FineractClient(
        base_url=base_url or settings.fineract_base_url,
        tenant=settings.fineract_tenant,
        username=settings.fineract_username,
        password=settings.fineract_password,
        timeout_seconds=30,
    )


@pytest.fixture
def fineract(settings: Settings) -> Iterator[FineractClient]:
    client = fineract_client(settings)
    yield client
    client.close()


@pytest.fixture
def erp(settings: Settings) -> Iterator[ErpClient]:
    client = ErpClient(settings.erp_base_url, settings.erp_api_key, timeout_seconds=10)
    yield client
    client.close()


class OneAccountErp:
    """An ERP chart with just the accounts given, to test cases the real chart lacks."""

    def __init__(self, *accounts: ErpAccount) -> None:
        self.accounts = list(accounts)

    def list_accounts(self, correlation_id: str | None = None) -> list[ErpAccount]:
        return list(self.accounts)


def detail_account(code: str, reference: str) -> ErpAccount:
    return ErpAccount(
        code=code,
        external_reference=reference,
        name=f"GL sync test {reference}",
        type="EXPENSE",
        usage="DETAIL",
        parent_code=None,
        currency="RWF",
        created_at=datetime.now(UTC),
    )


def erp_references(erp: ErpClient) -> list[str]:
    return [a.external_reference for a in erp.list_accounts()]


# --- API --------------------------------------------------------------------------------


def test_sync_needs_a_partner_key(client: FlaskClient) -> None:
    assert client.post("/api/accounts/sync").status_code == 401
    response = client.get("/api/accounts/sync")
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "UNAUTHENTICATED"


def test_report_before_any_sync_is_empty(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get("/api/accounts/sync", headers=auth)

    assert response.status_code == 200
    assert response.get_json() == {
        "accounts": [],
        "counts": {"SYNCED": 0, "SYNC_PENDING": 0, "FAILED": 0},
        "writes": 0,
        "in_progress": False,
    }


def test_sync_brings_the_whole_erp_chart_to_synced(
    client: FlaskClient, auth: dict[str, str], erp: ErpClient, fineract: FineractClient
) -> None:
    chart = erp.list_accounts()

    first = client.post("/api/accounts/sync", headers=auth)

    assert first.status_code == 200
    body = first.get_json()
    assert {a["erp_reference"] for a in body["accounts"]} == {a.external_reference for a in chart}
    assert {a["status"] for a in body["accounts"]} == {"SYNCED"}, body
    assert body["counts"] == {"SYNCED": len(chart), "SYNC_PENDING": 0, "FAILED": 0}
    assert set(body["accounts"][0]) == {
        "erp_reference",
        "code",
        "name",
        "status",
        "attempts",
        "last_error",
    }
    assert "gl_account_id" not in first.get_data(as_text=True).lower()
    assert "glaccountid" not in first.get_data(as_text=True).lower()

    # Fineract really holds each account under its ERP reference and code.
    for account in chart:
        mapping = fineract.get_erp_account(account.external_reference)
        assert mapping is not None
        assert mapping.gl_code == account.code

    second = client.post("/api/accounts/sync", headers=auth)
    assert second.get_json()["writes"] == 0
    assert second.get_json()["counts"]["SYNCED"] == len(chart)

    report = client.get("/api/accounts/sync", headers=auth).get_json()
    assert report["accounts"] == second.get_json()["accounts"]
    assert report["writes"] == 0


def test_four_syncs_leave_one_erp_account_one_gl_account_one_mapping(
    client: FlaskClient, auth: dict[str, str], erp: ErpClient, fineract: FineractClient, db: Engine
) -> None:
    for _ in range(4):
        assert client.post("/api/accounts/sync", headers=auth).status_code == 200

    gl_accounts = [a for a in fineract.get("/glaccounts") if a["glCode"] == "1200"]
    assert len(gl_accounts) == 1
    assert erp_references(erp).count("GL-1200") == 1
    with db.connect() as conn:
        rows = conn.execute(
            select(func.count()).select_from(gl_sync).where(gl_sync.c.erp_reference == "GL-1200")
        ).scalar_one()
    assert rows == 1


def test_four_syncs_at_once_send_each_account_once(
    app: Any, auth: dict[str, str], erp: ErpClient, fineract: FineractClient
) -> None:
    chart_size = len(erp.list_accounts())

    def post() -> dict[str, Any]:
        response = app.test_client().post("/api/accounts/sync", headers=auth)
        body: dict[str, Any] = response.get_json()
        # A call that found another run going answers 202 and changed nothing.
        assert response.status_code == (202 if body["in_progress"] else 200)
        return body

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: post(), range(4)))

    # Fresh table: each account is sent once in total, however the four runs overlap.
    assert sum(r["writes"] for r in results) == chart_size
    assert any(not r["in_progress"] for r in results)
    assert len([a for a in fineract.get("/glaccounts") if a["glCode"] == "1200"]) == 1


def test_erp_down_answers_erp_unavailable(settings: Settings, db: Engine) -> None:
    down = create_app(dataclasses.replace(settings, erp_base_url=UNREACHABLE), engine=db)

    response = down.test_client().post("/api/accounts/sync", headers={"X-API-Key": "test-key-a"})

    assert response.status_code == 503
    error = response.get_json()["error"]
    assert error["code"] == "ERP_UNAVAILABLE"
    assert error["correlation_id"] == response.headers["X-Correlation-ID"]


def test_fineract_down_reports_pending_then_a_later_sync_completes(
    settings: Settings, db: Engine, auth: dict[str, str], erp: ErpClient
) -> None:
    down = create_app(dataclasses.replace(settings, fineract_base_url=UNREACHABLE), engine=db)

    body = down.test_client().post("/api/accounts/sync", headers=auth).get_json()

    assert body["counts"]["SYNCED"] == 0
    assert body["counts"]["SYNC_PENDING"] == len(erp.list_accounts())
    assert "unavailable" in body["accounts"][0]["last_error"].lower()

    up = create_app(settings, engine=db)
    body = up.test_client().post("/api/accounts/sync", headers=auth).get_json()
    assert body["counts"]["SYNCED"] == len(erp.list_accounts())


@contextmanager
def another_sync_running(db: Engine) -> Iterator[None]:
    with db.connect() as other:
        assert other.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": GL_SYNC_LOCK_KEY})
        try:
            yield
        finally:
            other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": GL_SYNC_LOCK_KEY})


def test_sync_while_another_runs_answers_202_and_does_nothing(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    with another_sync_running(db):
        response = client.post("/api/accounts/sync", headers=auth)

    assert response.status_code == 202
    body = response.get_json()
    assert body["in_progress"] is True
    assert body["writes"] == 0
    assert client.get("/api/accounts/sync", headers=auth).status_code == 200


def test_one_account_is_synced_by_its_reference(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.post("/api/accounts/GL-1000/sync", headers=auth)

    assert response.status_code == 200
    body = response.get_json()
    assert body["in_progress"] is False
    assert body["account"]["erp_reference"] == "GL-1000"
    assert body["account"]["status"] == "SYNCED", body
    report = client.get("/api/accounts/sync", headers=auth).get_json()
    assert [a["erp_reference"] for a in report["accounts"]] == ["GL-1000"]
    # Again: same reference, nothing new to do.
    again = client.post("/api/accounts/GL-1000/sync", headers=auth)
    assert again.status_code == 200
    assert again.get_json()["account"] == body["account"]


def test_one_account_unknown_to_the_erp_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.post("/api/accounts/GL-NOPE-0/sync", headers=auth)

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "NOT_FOUND"


def test_one_account_while_a_sync_runs_answers_202_with_its_current_state(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    client.post("/api/accounts/GL-1000/sync", headers=auth)

    with another_sync_running(db):
        response = client.post("/api/accounts/GL-1000/sync", headers=auth)
        unknown_yet = client.post("/api/accounts/GL-1100/sync", headers=auth)

    assert response.status_code == 202
    assert response.get_json()["in_progress"] is True
    assert response.get_json()["account"]["status"] == "SYNCED"
    assert unknown_yet.status_code == 202
    assert unknown_yet.get_json() == {"account": None, "in_progress": True}


def erp_failing_with(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def fake_erp(base_url: str, api_key: str, timeout_seconds: float) -> ErpClient:
        return ErpClient(base_url, api_key, timeout_seconds, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("integration_service.api.gl_sync.ErpClient", fake_erp)


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("read timed out", request=request)


def server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR", "message": "boom"}})


@pytest.mark.parametrize(
    ("handler", "status", "code"),
    [(timeout, 504, "ERP_TIMEOUT"), (server_error, 502, "ERP_UNAVAILABLE")],
)
@pytest.mark.parametrize("path", ["/api/accounts/sync", "/api/accounts/GL-1000/sync"])
def test_erp_failures_map_to_their_answer(
    client: FlaskClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    status: int,
    code: str,
    path: str,
) -> None:
    erp_failing_with(monkeypatch, handler)

    response = client.post(path, headers=auth | {"X-Correlation-ID": "c-erp-test"})

    assert response.status_code == status
    error = response.get_json()["error"]
    assert error["code"] == code
    assert error["correlation_id"] == "c-erp-test"


# --- service against the real Fineract ---------------------------------------------------


def test_a_new_account_is_created_in_fineract(db: Engine, fineract: FineractClient) -> None:
    nonce = f"{secrets.randbelow(10**7):07d}"
    account = detail_account(f"7{nonce}", f"IT-GLS-{nonce}")

    report = sync_all(db, OneAccountErp(account), fineract)

    assert report.counts["SYNCED"] == 1
    mapping = fineract.get_erp_account(account.external_reference)
    assert mapping is not None and mapping.gl_code == account.code
    assert sync_all(db, OneAccountErp(account), fineract).writes == 0


def test_a_gl_code_taken_in_fineract_fails_with_the_reason(
    db: Engine, fineract: FineractClient
) -> None:
    # Make the taken code ourselves, so this works on any Fineract, even a fresh one.
    nonce = f"{secrets.randbelow(10**7):07d}"
    owner = detail_account(f"7{nonce}", f"IT-GLS-OWNER-{nonce}")
    fineract.put_erp_account(owner.external_reference, build_fineract_gl_body(owner))
    account = detail_account(owner.code, f"IT-GLS-TAKEN-{nonce}")

    report = sync_all(db, OneAccountErp(account), fineract)

    assert report.counts["FAILED"] == 1
    assert report.accounts[0].last_error is not None
    assert report.accounts[0].last_error.startswith("GL_CODE_TAKEN")
    assert fineract.get_erp_account(account.external_reference) is None


def test_the_spec_documents_the_sync_routes(client: FlaskClient) -> None:
    spec = client.get("/openapi/openapi.json").get_json()
    sync = spec["paths"]["/api/accounts/sync"]

    assert sync["post"]["tags"] == ["gl-sync"]
    ok = sync["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ok.endswith("/SyncReportResponse")
    assert {"401", "422", "502", "503", "504"} <= set(sync["post"]["responses"])
    assert sync["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == ok
    accepted = sync["post"]["responses"]["202"]["content"]["application/json"]["schema"]["$ref"]
    assert accepted == ok
    one = spec["paths"]["/api/accounts/{erp_reference}/sync"]["post"]
    assert {"200", "202", "401", "404", "502", "503", "504"} <= set(one["responses"])
    assert "gl_account_id" not in str(spec["components"]["schemas"]["AccountSyncItem"])
