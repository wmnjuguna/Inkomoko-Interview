"""The outbox against the real Postgres and the real Mock ERP.

Operations are seeded with the idempotency engine (no Fineract needed). Faults are switched
on the Mock ERP with /admin/faults, only for /api/journal-entries and always switched off
again in a finally. ERP references are unique per run, because the ERP keeps its entries."""

from __future__ import annotations

import os
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select, text

from integration_service.clients.erp import ErpClient
from integration_service.mapping.journal import (
    FineractTransaction,
    GlRoleAccounts,
    plan_entry,
)
from integration_service.models import operation_events, operations, outbox
from integration_service.services.operations import New, begin
from integration_service.services.outbox import record_money_done, send_due

ERP_URL = os.environ.get("ERP_BASE_URL", "http://localhost:8090")
ERP_KEY = os.environ.get("ERP_API_KEY", "change-me-erp-key")
ACCOUNTS = GlRoleAccounts("1100", "1200", "4100", "2100")
JOURNAL_PATH = "/api/journal-entries"


# --- fixtures and helpers --------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def no_fault_left() -> Iterator[None]:
    yield
    _faults("DELETE")


def _faults(method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = httpx.request(
        method, f"{ERP_URL}/admin/faults", json=body, headers={"X-API-Key": ERP_KEY}
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


@contextmanager
def erp_fault(**setting: Any) -> Iterator[None]:
    _faults("PUT", {"path_prefix": JOURNAL_PATH, **setting})
    try:
        yield
    finally:
        _faults("DELETE")


class CountingErp:
    """Wraps the real ERP client and counts journal posts per reference."""

    def __init__(self, inner: ErpClient, delay: float = 0.0) -> None:
        self.inner = inner
        self.delay = delay
        self.calls: Counter[str] = Counter()
        self._lock = threading.Lock()

    def post_journal_entry(
        self, entry: dict[str, Any], correlation_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self.calls[entry["external_reference"]] += 1
        if self.delay:
            threading.Event().wait(self.delay)
        return self.inner.post_journal_entry(entry, correlation_id)


def run_id() -> str:
    return uuid.uuid4().hex[:10]


def fineract_txn(op_key: str, kind: str = "DSB", amount: int = 1200000) -> FineractTransaction:
    code = "disbursement" if kind == "DSB" else "repayment"
    body: dict[str, Any] = {
        "type": {"code": f"loanTransactionType.{code}"},
        "date": date.today().isoformat(),
        "amount": float(amount),
        "externalId": op_key,
        "currency": {"code": "RWF"},
    }
    if kind == "RPY":
        body["principalPortion"] = float(amount)
    return FineractTransaction.model_validate(body)


def seed(
    engine: Engine,
    kind: str = "DSB",
    reference: str | None = None,
    txn: FineractTransaction | None = None,
) -> int:
    """An operation whose Fineract part is done and whose entry waits in the outbox."""
    reference = reference or f"OBX-{run_id()}"
    loan = f"LN-2026-{run_id()}"
    started = begin(
        engine, kind, reference, {"r": reference}, "partner-a", "c-seed", loan_reference=loan
    )
    assert isinstance(started, New)
    op = started.operation
    plan = plan_entry(kind, op.op_key, txn or fineract_txn(op.op_key, kind), ACCOUNTS, loan)
    with engine.begin() as conn:
        record_money_done(
            conn,
            op.id,
            plan,
            response_status=200,
            response_body={"external_reference": reference, "erp_status": plan.erp_status},
            fineract_resource_id=4,
            fineract_transaction_id=17,
            correlation_id="c-seed",
        )
    return op.id


def outbox_row(engine: Engine, op_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(select(outbox).where(outbox.c.operation_id == op_id)).mappings().one()
    return dict(row)


def op_row(engine: Engine, op_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(select(operations).where(operations.c.id == op_id)).mappings().one()
    return dict(row)


def events(engine: Engine, op_id: int) -> list[tuple[str | None, str, str | None]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                operation_events.c.from_status,
                operation_events.c.to_status,
                operation_events.c.detail,
            )
            .where(operation_events.c.operation_id == op_id)
            .order_by(operation_events.c.id)
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def erp_entries(reference: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{ERP_URL}{JOURNAL_PATH}",
        params={"reference": reference},
        headers={"X-API-Key": ERP_KEY},
    )
    response.raise_for_status()
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def now() -> datetime:
    return datetime.now(UTC)


# --- record_money_done ------------------------------------------------------------------------


def test_record_money_done_adds_the_outbox_row_in_the_same_transaction(eng: Engine) -> None:
    op_id = seed(eng)
    row = outbox_row(eng, op_id)
    op = op_row(eng, op_id)
    assert row["status"] == "PENDING"
    assert row["entry"]["external_reference"] == op["op_key"]
    assert row["entry"]["lines"] == [
        {"account_code": "1200", "debit": 1200000},
        {"account_code": "1100", "credit": 1200000},
    ]
    assert op["status"] == "ERP_PENDING"
    assert op["erp_status"] == "PENDING"
    assert op["fineract_transaction_id"] == 17


def test_an_entry_that_cannot_be_built_needs_reconciliation_and_is_never_sent(
    eng: Engine, erp: ErpClient
) -> None:
    reference = f"OBX-{run_id()}"
    txn = FineractTransaction.model_validate(
        {
            "type": {"code": "loanTransactionType.repayment"},
            "date": date.today().isoformat(),
            "amount": 1000.0,
            "principalPortion": 900.0,
            "feeChargesPortion": 100.0,
            "externalId": f"RPY:{reference}",
        }
    )
    op_id = seed(eng, "RPY", reference, txn)
    row = outbox_row(eng, op_id)
    op = op_row(eng, op_id)
    assert row["status"] == "NEEDS_RECONCILIATION"
    assert row["entry"]["unbuilt"] is True
    assert op["status"] == "NEEDS_RECONCILIATION"
    assert op["erp_status"] == "NEEDS_RECONCILIATION"
    assert "fee or penalty" in op["last_error"]
    assert op["response_body"] == {
        "external_reference": reference,
        "erp_status": "NEEDS_RECONCILIATION",
    }
    counting = CountingErp(erp)
    report = send_due(eng, counting, now())
    assert report.claimed == 0
    assert not counting.calls


def test_a_failure_after_the_outbox_insert_leaves_nothing(eng: Engine) -> None:
    reference = f"OBX-{run_id()}"
    started = begin(eng, "DSB", reference, {}, "partner-a", None, loan_reference="LN-2026-1")
    assert isinstance(started, New)
    op = started.operation
    plan = plan_entry("DSB", op.op_key, fineract_txn(op.op_key), ACCOUNTS, "LN-2026-1")
    with pytest.raises(RuntimeError), eng.begin() as conn:
        record_money_done(conn, op.id, plan, response_status=200, response_body={})
        raise RuntimeError("crash before commit")
    assert op_row(eng, op.id)["status"] == "FINERACT_PENDING"
    with eng.connect() as conn:
        assert conn.execute(select(outbox).where(outbox.c.operation_id == op.id)).first() is None


# --- delivery ----------------------------------------------------------------------------------


def test_delivers_once_and_completes_the_operation(eng: Engine, erp: ErpClient) -> None:
    op_id = seed(eng)
    counting = CountingErp(erp)
    report = send_due(eng, counting, now())
    assert (report.claimed, report.sent) == (1, 1)

    row = outbox_row(eng, op_id)
    op = op_row(eng, op_id)
    assert row["status"] == "SENT"
    assert row["attempts"] == 1
    assert row["erp_transaction_id"]
    assert op["status"] == "COMPLETED"
    assert op["erp_status"] == "SENT"
    assert op["erp_transaction_id"] == row["erp_transaction_id"]
    assert events(eng, op_id)[-1][:2] == ("ERP_PENDING", "COMPLETED")

    again = send_due(eng, counting, now() + timedelta(hours=1))
    assert again.claimed == 0
    assert counting.calls == Counter({op["op_key"]: 1})
    stored = erp_entries(op["op_key"])
    assert len(stored) == 1
    assert stored[0]["erp_transaction_id"] == row["erp_transaction_id"]
    assert stored[0]["source"] == "DISBURSEMENT"
    assert stored[0]["total"] == 1200000


def test_a_row_not_yet_due_is_left_alone(eng: Engine, erp: ErpClient) -> None:
    op_id = seed(eng)
    counting = CountingErp(erp)
    report = send_due(eng, counting, now() - timedelta(minutes=5))
    assert report.claimed == 0
    assert outbox_row(eng, op_id)["status"] == "PENDING"


def test_erp_down_is_retried_after_the_backoff_then_delivered(eng: Engine, erp: ErpClient) -> None:
    op_id = seed(eng)
    start = now()
    with erp_fault(mode="down"):
        first = send_due(eng, erp, start, rand=lambda: 0.0)
        assert (first.claimed, first.retried) == (1, 1)
        row = outbox_row(eng, op_id)
        assert row["status"] == "PENDING"
        assert row["attempts"] == 1
        assert row["next_attempt_at"] == start + timedelta(seconds=1)
        assert "not reachable" in row["last_error"]
        # Not due again before the backoff ends.
        assert send_due(eng, erp, start + timedelta(milliseconds=500)).claimed == 0
        second = send_due(eng, erp, start + timedelta(seconds=1), rand=lambda: 0.0)
        assert second.retried == 1
        assert outbox_row(eng, op_id)["next_attempt_at"] == start + timedelta(seconds=3)

    third = send_due(eng, erp, start + timedelta(seconds=3))
    assert third.sent == 1
    assert outbox_row(eng, op_id)["attempts"] == 3
    assert op_row(eng, op_id)["status"] == "COMPLETED"
    retries = [e for e in events(eng, op_id) if e[0] == e[1] == "ERP_PENDING"]
    assert len(retries) == 2


def test_too_many_failures_need_reconciliation(eng: Engine, erp: ErpClient) -> None:
    op_id = seed(eng)
    start = now()
    with erp_fault(mode="down"):
        send_due(eng, erp, start, max_attempts=2)
        last = send_due(eng, erp, start + timedelta(minutes=5), max_attempts=2)
    assert last.needs_reconciliation == 1
    row = outbox_row(eng, op_id)
    op = op_row(eng, op_id)
    assert row["status"] == "NEEDS_RECONCILIATION"
    assert "2 attempts" in row["last_error"]
    assert op["status"] == "NEEDS_RECONCILIATION"
    assert op["erp_status"] == "NEEDS_RECONCILIATION"
    assert events(eng, op_id)[-1][:2] == ("ERP_PENDING", "NEEDS_RECONCILIATION")
    assert send_due(eng, erp, start + timedelta(hours=1)).claimed == 0


def test_rate_limit_honours_retry_after(eng: Engine, erp: ErpClient) -> None:
    op_id = seed(eng)
    start = now()
    with erp_fault(mode="rate_limited", retry_after=7, remaining=1):
        report = send_due(eng, erp, start)
    assert report.rate_limited == 1
    row = outbox_row(eng, op_id)
    assert row["status"] == "PENDING"
    assert row["next_attempt_at"] == start + timedelta(seconds=7)
    assert row["attempts"] == 0, "a 429 is not a failed attempt"
    assert send_due(eng, erp, start + timedelta(seconds=6)).claimed == 0
    assert send_due(eng, erp, start + timedelta(seconds=7)).sent == 1


def test_erp_conflict_needs_reconciliation_at_once(eng: Engine, erp: ErpClient) -> None:
    reference = f"OBX-{run_id()}"
    erp.post_journal_entry(
        {
            "external_reference": f"DSB:{reference}",
            "entry_date": date.today().isoformat(),
            "source": "MANUAL",
            "description": "someone else used this reference",
            "lines": [
                {"account_code": "1100", "debit": 1},
                {"account_code": "1200", "credit": 1},
            ],
        }
    )
    op_id = seed(eng, "DSB", reference)
    report = send_due(eng, erp, now())
    assert report.needs_reconciliation == 1
    row = outbox_row(eng, op_id)
    assert row["status"] == "NEEDS_RECONCILIATION"
    assert row["attempts"] == 1
    assert "409" in row["last_error"]
    assert op_row(eng, op_id)["status"] == "NEEDS_RECONCILIATION"
    assert "409" in op_row(eng, op_id)["last_error"]


def test_an_entry_the_erp_refuses_needs_reconciliation(eng: Engine, erp: ErpClient) -> None:
    op_id = seed(eng)
    with eng.begin() as conn:
        entry = conn.execute(
            select(outbox.c.entry).where(outbox.c.operation_id == op_id)
        ).scalar_one()
        entry["lines"][0]["account_code"] = "9999"  # not in the ERP chart
        conn.execute(outbox.update().where(outbox.c.operation_id == op_id).values(entry=entry))
    report = send_due(eng, erp, now())
    assert report.needs_reconciliation == 1
    assert op_row(eng, op_id)["status"] == "NEEDS_RECONCILIATION"


def test_timeout_after_write_retry_gets_200_and_one_entry(eng: Engine) -> None:
    op_id = seed(eng)
    op_key = op_row(eng, op_id)["op_key"]
    impatient = ErpClient(ERP_URL, ERP_KEY, timeout_seconds=1.0)
    try:
        start = now()
        with erp_fault(mode="timeout_after_write", delay_seconds=3, remaining=1):
            first = send_due(eng, impatient, start, rand=lambda: 0.0)
        assert first.retried == 1
        assert "no answer" in outbox_row(eng, op_id)["last_error"]
        second = send_due(eng, impatient, start + timedelta(seconds=1))
        assert second.sent == 1
    finally:
        impatient.close()
    stored = erp_entries(op_key)
    assert len(stored) == 1
    assert outbox_row(eng, op_id)["erp_transaction_id"] == stored[0]["erp_transaction_id"]
    assert op_row(eng, op_id)["status"] == "COMPLETED"


def test_two_senders_at_once_never_send_twice(eng: Engine, erp: ErpClient) -> None:
    ids = [seed(eng, "RPY", amount_ref) for amount_ref in (f"OBX-{run_id()}" for _ in range(12))]
    counting = CountingErp(erp, delay=0.05)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def sender() -> None:
        try:
            barrier.wait()
            for _ in range(3):
                send_due(eng, counting, now(), batch_size=3)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=sender) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(counting.calls) == 12
    assert set(counting.calls.values()) == {1}
    assert {outbox_row(eng, i)["status"] for i in ids} == {"SENT"}
    assert {op_row(eng, i)["status"] for i in ids} == {"COMPLETED"}


def test_a_result_for_a_row_claimed_again_is_not_recorded(eng: Engine, erp: ErpClient) -> None:
    """A sender whose lease ran out must not overwrite the newer claim's result."""
    op_id = seed(eng)

    class Slow:
        def post_journal_entry(
            self, entry: dict[str, Any], correlation_id: str | None = None
        ) -> dict[str, Any]:
            # While this call runs, another sender claims the row again (the lease ended).
            with eng.begin() as conn:
                conn.execute(
                    outbox.update()
                    .where(outbox.c.operation_id == op_id)
                    .values(attempts=outbox.c.attempts + 1)
                )
            return erp.post_journal_entry(entry, correlation_id)

    report = send_due(eng, Slow(), now())
    assert report.stale == 1
    row = outbox_row(eng, op_id)
    assert row["status"] == "PENDING"
    assert row["attempts"] == 2


def test_a_lease_that_runs_out_mid_batch_is_never_sent_twice(eng: Engine, erp: ErpClient) -> None:
    """A slow ERP makes the batch outlive its lease. The second sender may take over the
    rows still waiting, but no entry is ever posted by both senders."""
    ids = [seed(eng, "RPY") for _ in range(3)]
    slow = CountingErp(erp, delay=0.4)
    fast = CountingErp(erp)
    errors: list[BaseException] = []

    def first() -> None:
        try:
            send_due(eng, slow, batch_size=3, lease_seconds=0.5)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=first)
    worker.start()
    threading.Event().wait(0.7)  # the lease of the rows not yet sent has run out
    send_due(eng, fast, batch_size=3, lease_seconds=0.5)
    worker.join()
    assert not errors
    both = set(slow.calls) & set(fast.calls)
    assert both == set(), f"posted by both senders: {sorted(both)}"
    assert sum(slow.calls.values()) + sum(fast.calls.values()) == 3
    assert {outbox_row(eng, i)["status"] for i in ids} == {"SENT"}
