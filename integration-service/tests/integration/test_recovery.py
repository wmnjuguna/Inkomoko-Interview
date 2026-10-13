"""Recovery against the real Postgres, with a fake Fineract (and the real Mock ERP for the
outbox part). An operation left in FINERACT_PENDING (a crash, or an unclear answer whose
lookup failed) is looked up by its externalId and finished the way the normal path would,
or marked FAILED when Fineract has no record of it, or left for later."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine, select, text, update

from integration_service.app import create_app
from integration_service.clients.erp import ErpClient
from integration_service.clients.errors import FineractNotFound, FineractTimeout
from integration_service.config import Settings
from integration_service.mapping.journal import GlRoleAccounts, GlRolesConfigError
from integration_service.models import clients, loans, operations, outbox
from integration_service.services.loan_locks import loan_lock
from integration_service.services.operations import New, begin
from integration_service.services.recovery import BackgroundLoop, recover_once

ERP_URL = os.environ.get("ERP_BASE_URL", "http://localhost:8090")
ERP_KEY = os.environ.get("ERP_API_KEY", "change-me-erp-key")
DISBURSEMENT_KEYS = {
    "loan_reference",
    "client_uid",
    "external_reference",
    "type",
    "status",
    "amount",
    "transaction_date",
    "erp_status",
    "loan_status",
}
REPAYMENT_KEYS = DISBURSEMENT_KEYS | {
    "principal_portion",
    "interest_portion",
    "overpayment_portion",
    "fees_portion",
    "penalties_portion",
    "outstanding_after",
}
ID_LIKE = re.compile(r"(^id$|_id$|Id$)")
ACCOUNTS = GlRoleAccounts("1100", "1200", "4100", "2100")


class FakeFineract:
    """Answers GETs from a dict of path -> body or exception. Unknown paths are 404."""

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}
        self.calls: list[str] = []
        self.delay = 0.0
        self._lock = threading.Lock()

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        with self._lock:
            self.calls.append(path)
        if self.delay:
            time.sleep(self.delay)
        answer = self.answers.get(path)
        if answer is None:
            raise FineractNotFound(404, {"developerMessage": "Transaction 991 not found"})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        raise AssertionError("recovery never writes to Fineract")


@pytest.fixture
def eng(engine: Engine) -> Engine:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE outbox, operation_events, loans, clients, operations "
                "RESTART IDENTITY CASCADE"
            )
        )
    return engine


@pytest.fixture
def erp() -> Iterator[ErpClient]:
    client = ErpClient(ERP_URL, ERP_KEY, timeout_seconds=10.0)
    yield client
    client.close()


@pytest.fixture
def fineract() -> FakeFineract:
    return FakeFineract()


def rid() -> str:
    return uuid.uuid4().hex[:10]


def shows_id(value: Any, fake_id: int) -> bool:
    """True if a Fineract id shows up: an id-like key anywhere, the id as a whole value, or
    the id as a separate word in a text. Our random hex references never count."""
    if isinstance(value, dict):
        return any(
            ID_LIKE.search(str(key)) or shows_id(inner, fake_id) for key, inner in value.items()
        )
    if isinstance(value, list):
        return any(shows_id(inner, fake_id) for inner in value)
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value == fake_id
    if isinstance(value, str):
        return re.search(rf"(?<![0-9A-Za-z-]){fake_id}(?![0-9A-Za-z-])", value) is not None
    return False


def stuck(
    engine: Engine,
    op_type: str = "DSB",
    *,
    age_seconds: float = 600,
    client_uid: str | None = None,
    loan_reference: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An operation left in FINERACT_PENDING `age_seconds` ago."""
    reference = f"REC-{rid()}"
    if op_type in ("APR", "DSB", "RPY", "APP") and loan_reference is None:
        loan_reference = f"LN-2026-{rid()}"
    if op_type == "CLI" and client_uid is None:
        client_uid = f"SME-{rid()}"
    if payload is None and op_type == "CLI":
        payload = {
            "external_reference": reference,
            "client_uid": client_uid,
            "first_name": "Aline",
            "last_name": "Uwase",
        }
    started = begin(
        engine,
        op_type,
        reference,
        payload or {"r": reference},
        "partner-a",
        "c-stuck",
        client_uid=client_uid,
        loan_reference=loan_reference,
    )
    assert isinstance(started, New)
    with engine.begin() as conn:
        conn.execute(
            update(operations)
            .where(operations.c.id == started.operation.id)
            .values(updated_at=datetime.now(UTC) - timedelta(seconds=age_seconds))
        )
    return op_row(engine, started.operation.id)


def op_row(engine: Engine, op_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(
            conn.execute(select(operations).where(operations.c.id == op_id)).mappings().one()
        )


def outbox_of(engine: Engine, op_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(select(outbox).where(outbox.c.operation_id == op_id)).mappings().first()
    return dict(row) if row else None


def txn_path(op: dict[str, Any]) -> str:
    return (
        f"/loans/external-id/{op['loan_reference']}/transactions/external-id/"
        f"{op['op_key'].replace(':', '%3A')}"
    )


def fineract_txn(op: dict[str, Any], **changes: Any) -> dict[str, Any]:
    disbursing = op["op_type"] == "DSB"
    body: dict[str, Any] = {
        "id": 991,
        "loanId": 77,
        "type": {
            "code": "loanTransactionType.disbursement"
            if disbursing
            else "loanTransactionType.repayment"
        },
        "date": [date.today().year, date.today().month, date.today().day],
        "currency": {"code": "RWF"},
        "amount": 1200000.0 if disbursing else 100000.0,
        "principalPortion": 0 if disbursing else 82000.0,
        "interestPortion": 0 if disbursing else 18000.0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
        "externalId": op["op_key"],
        "manuallyReversed": False,
    }
    body.update(changes)
    return body


def run(engine: Engine, fineract: FakeFineract, erp: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("accounts", lambda: ACCOUNTS)
    return recover_once(engine, fineract, erp, **kwargs)


class NoErp:
    """For tests that must not reach the ERP."""

    def post_journal_entry(self, entry: dict[str, Any], correlation_id: str | None = None) -> Any:
        raise AssertionError("no ERP call expected")


# --- money operations -----------------------------------------------------------------------------


def test_a_disbursement_found_in_fineract_is_finished_like_the_normal_path(
    eng: Engine, fineract: FakeFineract, erp: ErpClient
) -> None:
    op = stuck(eng, "DSB")
    fineract.answers[txn_path(op)] = fineract_txn(op, amount=750000.0)

    report = run(eng, fineract, erp)

    assert report.operations.checked == 1
    assert report.operations.completed == 1
    assert report.outbox.sent == 1
    row = op_row(eng, op["id"])
    assert row["status"] == "COMPLETED"
    assert row["erp_status"] == "SENT"
    assert row["fineract_transaction_id"] == 991
    assert row["fineract_resource_id"] == 77
    assert row["response_status"] == 200
    assert row["response_body"]["external_reference"] == op["external_reference"]
    assert set(row["response_body"]) == DISBURSEMENT_KEYS  # the route's own shape
    assert row["response_body"]["amount"] == 750000
    assert not shows_id(row["response_body"], 991)
    entry = outbox_of(eng, op["id"])
    assert entry is not None
    # Built from the FOUND transaction: 750 000, not anything we computed.
    assert entry["entry"]["lines"] == [
        {"account_code": "1200", "debit": 750000},
        {"account_code": "1100", "credit": 750000},
    ]
    assert entry["entry"]["external_reference"] == op["op_key"]


def test_a_repayment_found_is_saved_with_the_routes_answer(
    eng: Engine, fineract: FakeFineract
) -> None:
    op = stuck(eng, "RPY")
    fineract.answers[txn_path(op)] = fineract_txn(op)
    run(eng, fineract, NoErp(), send_outbox=False)
    body = op_row(eng, op["id"])["response_body"]
    assert set(body) == REPAYMENT_KEYS
    assert (body["type"], body["amount"]) == ("REPAYMENT", 100000)
    assert (body["principal_portion"], body["interest_portion"]) == (82000, 18000)
    assert body["status"] == "ERP_PENDING"


def test_a_repayment_found_with_a_fee_needs_reconciliation(
    eng: Engine, fineract: FakeFineract
) -> None:
    op = stuck(eng, "RPY")
    fineract.answers[txn_path(op)] = fineract_txn(op, amount=101000.0, feeChargesPortion=1000.0)
    report = run(eng, fineract, NoErp())
    assert report.operations.needs_reconciliation == 1
    row = op_row(eng, op["id"])
    assert row["status"] == "NEEDS_RECONCILIATION"
    assert row["erp_status"] == "NEEDS_RECONCILIATION"
    assert row["response_body"]["erp_status"] == "NEEDS_RECONCILIATION"
    # Fineract's own figures are kept even when no entry could be built.
    assert row["response_body"]["amount"] == 101000
    assert row["response_body"]["fees_portion"] == 1000


def test_a_reversed_transaction_needs_reconciliation(eng: Engine, fineract: FakeFineract) -> None:
    op = stuck(eng, "RPY")
    fineract.answers[txn_path(op)] = fineract_txn(op, manuallyReversed=True)
    run(eng, fineract, NoErp())
    assert op_row(eng, op["id"])["status"] == "NEEDS_RECONCILIATION"


def test_unreadable_gl_roles_still_record_what_fineract_did(
    eng: Engine, fineract: FakeFineract
) -> None:
    op = stuck(eng, "DSB")
    fineract.answers[txn_path(op)] = fineract_txn(op)

    def broken() -> GlRoleAccounts:
        raise GlRolesConfigError("Loan product file not found: /nope")

    run(eng, fineract, NoErp(), accounts=broken)
    row = op_row(eng, op["id"])
    assert row["status"] == "NEEDS_RECONCILIATION"
    assert row["fineract_transaction_id"] == 991


def test_not_in_fineract_means_failed_and_the_reference_can_retry(
    eng: Engine, fineract: FakeFineract
) -> None:
    op = stuck(eng, "DSB")
    report = run(eng, fineract, NoErp())
    assert report.operations.failed == 1
    row = op_row(eng, op["id"])
    assert row["status"] == "FAILED"
    assert "no record" in row["last_error"]
    assert not shows_id(row["last_error"], 991)
    again = begin(
        eng,
        "DSB",
        op["external_reference"],
        {"r": op["external_reference"]},
        "partner-a",
        None,
        loan_reference=op["loan_reference"],
    )
    assert isinstance(again, New)
    assert again.operation.attempts == 2


def test_a_failed_lookup_leaves_it_and_counts_an_attempt(
    eng: Engine, fineract: FakeFineract
) -> None:
    op = stuck(eng, "DSB")
    fineract.answers[txn_path(op)] = FineractTimeout("no answer")
    start = datetime.now(UTC)
    report = run(eng, fineract, NoErp(), now=start)
    assert report.operations.still_unknown == 1
    row = op_row(eng, op["id"])
    assert row["status"] == "FINERACT_PENDING"
    assert row["attempts"] == 1  # the Idempotency-Key attempt is not touched
    assert row["recovery_lookups"] == 1
    assert row["next_attempt_at"] > start
    assert "lookup failed" in row["last_error"]
    # Not looked up again before next_attempt_at.
    calls = len(fineract.calls)
    run(eng, fineract, NoErp(), now=start + timedelta(milliseconds=100))
    assert len(fineract.calls) == calls


def test_too_many_failed_lookups_need_reconciliation(eng: Engine, fineract: FakeFineract) -> None:
    op = stuck(eng, "DSB")
    fineract.answers[txn_path(op)] = FineractTimeout("no answer")
    start = datetime.now(UTC)
    for minute in range(3):
        run(eng, fineract, NoErp(), now=start + timedelta(minutes=minute), max_attempts=3)
    row = op_row(eng, op["id"])
    assert row["status"] == "NEEDS_RECONCILIATION"
    assert row["recovery_lookups"] == 3
    assert row["attempts"] == 1
    assert outbox_of(eng, op["id"]) is None


def test_partner_retries_do_not_use_up_the_lookup_budget(
    eng: Engine, fineract: FakeFineract
) -> None:
    """attempts is the Idempotency-Key attempt. Seven earlier partner retries must not make
    the first failed lookup the last one."""
    op = stuck(eng, "DSB")
    with eng.begin() as conn:
        conn.execute(update(operations).where(operations.c.id == op["id"]).values(attempts=7))
    fineract.answers[txn_path(op)] = FineractTimeout("no answer")
    report = run(eng, fineract, NoErp(), max_attempts=8)
    assert report.operations.still_unknown == 1
    row = op_row(eng, op["id"])
    assert row["status"] == "FINERACT_PENDING"
    assert (row["attempts"], row["recovery_lookups"]) == (7, 1)


def test_a_young_operation_is_left_to_its_request(eng: Engine, fineract: FakeFineract) -> None:
    op = stuck(eng, "DSB", age_seconds=5)
    report = run(eng, fineract, NoErp(), stale_after_seconds=60)
    assert report.operations.checked == 0
    assert fineract.calls == []
    assert op_row(eng, op["id"])["status"] == "FINERACT_PENDING"


def test_a_loan_being_written_right_now_is_skipped(eng: Engine, fineract: FakeFineract) -> None:
    op = stuck(eng, "DSB")
    with loan_lock(eng, op["loan_reference"]):
        report = run(eng, fineract, NoErp(), lock_wait_seconds=0.2)
    assert report.operations.skipped_busy == 1
    assert fineract.calls == []
    assert op_row(eng, op["id"])["status"] == "FINERACT_PENDING"
    assert op_row(eng, op["id"])["attempts"] == 1


def test_two_recoveries_at_once_look_each_operation_up_once(
    eng: Engine, fineract: FakeFineract
) -> None:
    ops = [stuck(eng, "RPY") for _ in range(6)]
    for op in ops:
        fineract.answers[txn_path(op)] = fineract_txn(op)
    fineract.delay = 0.1  # the passes overlap while lookups run
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            run(eng, fineract, NoErp(), send_outbox=False, batch_size=2)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    lookups = [call for call in fineract.calls if "/transactions/" in call]
    assert sorted(lookups) == sorted(txn_path(op) for op in ops)
    assert {op_row(eng, op["id"])["status"] for op in ops} == {"ERP_PENDING"}


# --- client, application, approval ----------------------------------------------------------------


def client_body(client_uid: str) -> dict[str, Any]:
    return {
        "id": 55,
        "externalId": client_uid,
        "active": True,
        "status": {"id": 300, "code": "clientStatusType.active", "value": "Active"},
        "firstname": "Aline",
        "lastname": "Uwase",
        "displayName": "Aline Uwase",
    }


def test_a_client_found_in_fineract_is_completed_and_saved(
    eng: Engine, fineract: FakeFineract
) -> None:
    op = stuck(eng, "CLI")
    fineract.answers[f"/clients/external-id/{op['client_uid']}"] = client_body(op["client_uid"])
    report = run(eng, fineract, NoErp())
    assert report.operations.completed == 1
    row = op_row(eng, op["id"])
    assert row["status"] == "COMPLETED"
    assert row["response_status"] == 201
    assert row["response_body"]["client_uid"] == op["client_uid"]
    assert not shows_id(row["response_body"], 55)
    with eng.connect() as conn:
        saved = (
            conn.execute(select(clients).where(clients.c.client_uid == op["client_uid"]))
            .mappings()
            .one()
        )
    assert saved["display_name"] == "Aline Uwase"
    assert saved["fineract_client_id"] == 55


def test_a_client_with_another_name_is_not_claimed(eng: Engine, fineract: FakeFineract) -> None:
    """Fineract holds the client_uid, but under another name: someone else's client. The
    route would say CLIENT_UID_TAKEN; recovery must not record it as ours."""
    op = stuck(
        eng,
        "CLI",
        payload={"first_name": "Jean", "last_name": "Habimana", "client_uid": "x"},
    )
    fineract.answers[f"/clients/external-id/{op['client_uid']}"] = client_body(op["client_uid"])
    report = run(eng, fineract, NoErp())
    assert report.operations.failed == 1
    row = op_row(eng, op["id"])
    assert row["status"] == "FAILED"
    assert row["response_body"] is None
    assert "already held" in row["last_error"]
    with eng.connect() as conn:
        assert conn.execute(select(clients)).first() is None


def test_a_client_without_the_requested_name_on_record_needs_a_manual_check(
    eng: Engine, fineract: FakeFineract
) -> None:
    """A row saved before the request payload was kept: no name to compare with."""
    op = stuck(eng, "CLI", payload={"r": "no names"})
    fineract.answers[f"/clients/external-id/{op['client_uid']}"] = client_body(op["client_uid"])
    report = run(eng, fineract, NoErp())
    assert report.operations.needs_reconciliation == 1
    assert op_row(eng, op["id"])["status"] == "NEEDS_RECONCILIATION"
    with eng.connect() as conn:
        assert conn.execute(select(clients)).first() is None


def test_a_client_not_in_fineract_is_failed(eng: Engine, fineract: FakeFineract) -> None:
    op = stuck(eng, "CLI")
    run(eng, fineract, NoErp())
    assert op_row(eng, op["id"])["status"] == "FAILED"


def loan_body(op: dict[str, Any], approved: bool, client_uid: str) -> dict[str, Any]:
    timeline: dict[str, Any] = {"submittedOnDate": [2026, 10, 13]}
    if approved:
        timeline["approvedOnDate"] = [2026, 10, 13]
    return {
        "id": 77,
        "externalId": op["loan_reference"],
        "clientExternalId": client_uid,
        "principal": 1200000.0,
        "status": {"id": 200 if approved else 100, "value": "Approved" if approved else "x"},
        "timeline": timeline,
    }


def add_client(engine: Engine, client_uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            clients.insert().values(
                client_uid=client_uid,
                external_reference=f"C-{client_uid}",
                display_name="Aline Uwase",
                status="ACTIVE",
            )
        )


def test_an_application_found_in_fineract_is_completed(eng: Engine, fineract: FakeFineract) -> None:
    client_uid = f"SME-{rid()}"
    add_client(eng, client_uid)
    op = stuck(eng, "APP", client_uid=client_uid)
    path = f"/loans/external-id/{op['loan_reference']}"
    fineract.answers[path] = loan_body(op, approved=False, client_uid=client_uid)
    report = run(eng, fineract, NoErp())
    assert report.operations.completed == 1
    row = op_row(eng, op["id"])
    assert row["status"] == "COMPLETED"
    assert row["response_status"] == 201
    assert row["response_body"]["loan_reference"] == op["loan_reference"]
    with eng.connect() as conn:
        saved = (
            conn.execute(select(loans).where(loans.c.loan_reference == op["loan_reference"]))
            .mappings()
            .one()
        )
    assert saved["principal"] == 1200000
    assert saved["fineract_loan_id"] == 77


@pytest.mark.parametrize(("approved", "status"), [(True, "COMPLETED"), (False, "FAILED")])
def test_an_approval_is_done_only_when_fineract_shows_it_approved(
    eng: Engine, fineract: FakeFineract, approved: bool, status: str
) -> None:
    op = stuck(eng, "APR")
    path = f"/loans/external-id/{op['loan_reference']}"
    fineract.answers[path] = loan_body(op, approved=approved, client_uid="SME-1")
    run(eng, fineract, NoErp())
    assert op_row(eng, op["id"])["status"] == status


# --- outbox inside the same pass ------------------------------------------------------------------


def test_recovery_also_sends_due_outbox_rows(
    eng: Engine, fineract: FakeFineract, erp: ErpClient
) -> None:
    op = stuck(eng, "RPY")
    fineract.answers[txn_path(op)] = fineract_txn(op)
    run(eng, fineract, erp, send_outbox=False)
    assert op_row(eng, op["id"])["status"] == "ERP_PENDING"
    report = run(eng, fineract, erp)
    assert report.operations.checked == 0
    assert report.outbox.sent == 1
    assert op_row(eng, op["id"])["status"] == "COMPLETED"


def test_report_holds_counts_only(eng: Engine, fineract: FakeFineract) -> None:
    stuck(eng, "DSB")
    data = run(eng, fineract, NoErp()).as_dict()
    assert data["operations"]["failed"] == 1
    assert set(data) == {"operations", "outbox"}
    assert all(isinstance(v, int) for v in data["operations"].values())
    assert all(isinstance(v, int) for v in data["outbox"].values())


# --- the background loop --------------------------------------------------------------------------


def test_the_loop_keeps_going_after_an_error_and_stops_cleanly() -> None:
    ticks: list[int] = []

    def tick() -> None:
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("boom")

    loop = BackgroundLoop(tick, interval_seconds=0.02, name="test-loop")
    loop.start()
    deadline = time.monotonic() + 5
    while len(ticks) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    loop.stop(timeout=2)
    assert len(ticks) >= 3
    assert not loop.is_alive()
    count = len(ticks)
    time.sleep(0.1)
    assert len(ticks) == count


def test_the_loop_runs_once_at_start() -> None:
    ticked = threading.Event()
    loop = BackgroundLoop(ticked.set, interval_seconds=60, name="test-loop")
    loop.start()
    assert ticked.wait(2)
    loop.stop(timeout=2)
    assert not loop.is_alive()


# --- POST /admin/recover --------------------------------------------------------------------------


def test_admin_recover_needs_a_key(client: FlaskClient) -> None:
    assert client.post("/admin/recover").status_code == 401


def test_admin_recover_runs_one_pass_and_shows_counts(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE outbox, operation_events, loans, clients, operations "
                "RESTART IDENTITY CASCADE"
            )
        )
    response = client.post("/admin/recover", headers=auth)
    assert response.status_code == 200
    body = response.get_json()
    assert body["operations"]["checked"] == 0
    assert "sent" in body["outbox"]


# --- the app factory ------------------------------------------------------------------------------


def test_the_app_starts_no_loop_when_disabled(client: FlaskClient) -> None:
    app = client.application
    assert "integration_service.recovery_loop" not in app.extensions


def test_the_app_starts_one_loop_when_enabled(settings: Settings, db: Engine) -> None:
    enabled = replace(settings, outbox_enabled=True, outbox_poll_seconds=60)
    app = create_app(enabled, engine=db)
    loop = app.extensions["integration_service.recovery_loop"]
    try:
        assert loop.is_alive()
    finally:
        loop.stop(timeout=30)
    assert not loop.is_alive()
