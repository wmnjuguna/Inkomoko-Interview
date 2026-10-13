"""The idempotency engine against the real Postgres: begin(), move(), mark_fineract_done(),
the one-live-operation rules, and the per-loan lock. Everything is enforced by the database,
so these tests run several threads at once against it. No Fineract calls here."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError

from integration_service.errors import ErrorCode
from integration_service.models import operation_events, operations, outbox
from integration_service.services import operations as ops
from integration_service.services.loan_locks import LoanLockTimeoutError, loan_lock, loan_lock_key
from integration_service.services.operations import (
    BeginResult,
    Conflict,
    InProgress,
    New,
    Replay,
    ResponseAlreadySavedError,
    StaleStatusError,
    Taken,
    begin,
    list_for_reference,
    mark_failed,
    mark_fineract_done,
    move,
    next_loan_reference,
    save_response,
)
from integration_service.state_machine import InvalidTransition

LOAN = "LN-2026-000001"
PAYLOAD = {"loan_reference": LOAN, "amount": 500000}


def clear_operations(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE outbox, operation_events, loans, clients, operations "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def eng(engine: Engine) -> Engine:
    clear_operations(engine)
    return engine


def start(
    engine: Engine,
    reference: str = "FIN-1",
    op_type: str = "DSB",
    payload: dict[str, Any] | None = None,
    partner: str = "partner-a",
    loan_reference: str | None = LOAN,
    client_uid: str | None = None,
) -> BeginResult:
    return begin(
        engine,
        op_type,
        reference,
        PAYLOAD if payload is None else payload,
        partner,
        "c-test",
        client_uid=client_uid,
        loan_reference=loan_reference,
    )


def new(result: BeginResult) -> ops.Operation:
    assert isinstance(result, New), result
    return result.operation


def history(engine: Engine, operation_id: int) -> list[tuple[str | None, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(operation_events.c.from_status, operation_events.c.to_status)
            .where(operation_events.c.operation_id == operation_id)
            .order_by(operation_events.c.id)
        ).all()
    return [(r[0], r[1]) for r in rows]


def finish(
    engine: Engine, operation: ops.Operation, outbox_entry: dict[str, Any] | None = None
) -> None:
    with engine.begin() as conn:
        mark_fineract_done(
            conn,
            operation.id,
            response_status=200,
            response_body={"external_reference": operation.external_reference, "n": 1},
            fineract_resource_id=11,
            fineract_transaction_id=22,
            outbox_entry=outbox_entry,
        )


def at_once(n: int, work: Callable[[int], BeginResult]) -> list[BeginResult]:
    """Run work(i) in n threads that all start together."""
    barrier = threading.Barrier(n)

    def run(i: int) -> BeginResult:
        barrier.wait()
        return work(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


# --- begin(): a new request ----------------------------------------------------------------


def test_new_request_is_claimed_and_pending(eng: Engine) -> None:
    operation = new(start(eng))
    assert operation.op_key == "DSB:FIN-1"
    assert operation.status == "FINERACT_PENDING"
    assert operation.attempts == 1
    assert operation.idempotency_key == "DSB:FIN-1:1"
    assert operation.loan_reference == LOAN
    assert operation.partner == "partner-a"
    assert history(eng, operation.id) == [(None, "RECEIVED"), ("RECEIVED", "FINERACT_PENDING")]


def test_same_request_while_running_is_in_progress(eng: Engine) -> None:
    first = new(start(eng))
    again = start(eng)
    assert isinstance(again, InProgress)
    assert again.operation is not None
    assert again.operation.id == first.id
    assert again.operation.status == "FINERACT_PENDING"
    with eng.connect() as conn:
        assert conn.execute(select(func.count()).select_from(operations)).scalar() == 1


def test_same_reference_with_other_data_is_a_conflict(eng: Engine) -> None:
    new(start(eng))
    assert start(eng, payload={**PAYLOAD, "amount": 1}) == Conflict()


def test_same_reference_from_another_partner_is_a_conflict(eng: Engine) -> None:
    new(start(eng))
    assert start(eng, partner="partner-b") == Conflict()


def test_same_reference_for_another_type_is_a_separate_operation(eng: Engine) -> None:
    new(start(eng, op_type="DSB"))
    rpy = new(start(eng, op_type="RPY"))
    assert rpy.op_key == "RPY:FIN-1"


# --- finishing, and replaying the saved answer -------------------------------------------


def test_finished_without_erp_part_is_completed_and_replayed(eng: Engine) -> None:
    operation = new(start(eng, op_type="APR", reference="APR-1"))
    finish(eng, operation)
    with eng.connect() as conn:
        row = conn.execute(select(operations).where(operations.c.id == operation.id)).one()
        assert conn.execute(select(func.count()).select_from(outbox)).scalar() == 0
    assert row.status == "COMPLETED"
    assert row.fineract_resource_id == 11
    assert row.fineract_transaction_id == 22
    assert history(eng, operation.id)[-2:] == [
        ("FINERACT_PENDING", "FINERACT_DONE"),
        ("FINERACT_DONE", "COMPLETED"),
    ]
    assert start(eng, op_type="APR", reference="APR-1") == Replay(
        200, {"external_reference": "APR-1", "n": 1}
    )


def test_finished_with_a_journal_entry_waits_for_the_erp(eng: Engine) -> None:
    operation = new(start(eng))
    entry = {"external_reference": "DSB:FIN-1", "lines": [{"account_code": "1200", "debit": 5}]}
    finish(eng, operation, outbox_entry=entry)
    with eng.connect() as conn:
        row = conn.execute(select(operations).where(operations.c.id == operation.id)).one()
        box = conn.execute(select(outbox)).one()
    assert row.status == "ERP_PENDING"
    assert row.erp_status == "PENDING"
    assert box.operation_id == operation.id
    assert box.status == "PENDING"
    assert box.entry == entry
    assert history(eng, operation.id)[-2:] == [
        ("FINERACT_PENDING", "FINERACT_DONE"),
        ("FINERACT_DONE", "ERP_PENDING"),
    ]
    # Finished means the Fineract part is done: the repeat gets the saved answer.
    assert isinstance(start(eng), Replay)


def test_fineract_done_and_outbox_row_are_one_transaction(eng: Engine) -> None:
    operation = new(start(eng))
    with pytest.raises(RuntimeError), eng.begin() as conn:
        mark_fineract_done(
            conn,
            operation.id,
            response_status=200,
            response_body={"x": 1},
            outbox_entry={"lines": []},
        )
        raise RuntimeError("crash before commit")
    with eng.connect() as conn:
        row = conn.execute(select(operations).where(operations.c.id == operation.id)).one()
        assert conn.execute(select(func.count()).select_from(outbox)).scalar() == 0
    assert row.status == "FINERACT_PENDING"
    assert row.response_body is None
    assert isinstance(start(eng), InProgress)


def test_replay_body_is_exactly_the_saved_one(eng: Engine) -> None:
    operation = new(start(eng))
    body = {"b": [1, 2, {"z": None, "a": "é"}], "a": 500000, "erp_status": "PENDING"}
    with eng.begin() as conn:
        mark_fineract_done(
            conn, operation.id, response_status=200, response_body=body, outbox_entry={"l": 1}
        )
    # The live status moves on; the replay does not.
    with eng.begin() as conn:
        move(conn, operation.id, "COMPLETED", "the ERP saved it", erp_status="SENT")
    assert start(eng) == Replay(200, body)


def test_a_saved_answer_is_never_overwritten(eng: Engine) -> None:
    operation = new(start(eng))
    with eng.begin() as conn:
        save_response(conn, operation.id, 200, {"first": True})
    with pytest.raises(ResponseAlreadySavedError), eng.begin() as conn:
        save_response(conn, operation.id, 200, {"second": True})


# --- failure and retry with the same reference --------------------------------------------


def test_failed_request_is_retried_on_the_same_row_with_the_next_attempt(eng: Engine) -> None:
    first = new(start(eng))
    failed = mark_failed(eng, first.id, "Fineract not reachable; nothing was sent")
    assert failed.status == "FAILED"
    assert failed.last_error == "Fineract not reachable; nothing was sent"
    again = new(start(eng))
    assert again.id == first.id
    assert again.attempts == 2
    assert again.idempotency_key == "DSB:FIN-1:2"
    assert again.status == "FINERACT_PENDING"
    assert again.last_error is None
    assert history(eng, first.id)[-2:] == [
        ("FINERACT_PENDING", "FAILED"),
        ("FAILED", "FINERACT_PENDING"),
    ]


def test_failed_request_with_other_data_is_still_a_conflict(eng: Engine) -> None:
    first = new(start(eng))
    mark_failed(eng, first.id, "refused")
    assert start(eng, payload={**PAYLOAD, "amount": 2}) == Conflict()


# --- move(): the state machine, recorded --------------------------------------------------


def test_move_refuses_a_move_the_state_machine_does_not_allow(eng: Engine) -> None:
    operation = new(start(eng))
    with pytest.raises(InvalidTransition):
        move(eng, operation.id, "COMPLETED", "skip ahead")
    assert len(history(eng, operation.id)) == 2


def test_move_refuses_when_the_status_changed_underneath(eng: Engine) -> None:
    operation = new(start(eng))
    with pytest.raises(StaleStatusError):
        move(eng, operation.id, "FAILED", "late", expected="RECEIVED")


def test_move_records_detail_time_and_correlation_id(eng: Engine) -> None:
    operation = new(start(eng))
    move(eng, operation.id, "FAILED", "Fineract refused (400)", correlation_id="c-2")
    with eng.connect() as conn:
        event = conn.execute(
            select(operation_events).order_by(operation_events.c.id.desc()).limit(1)
        ).one()
    assert event.detail == "Fineract refused (400)"
    assert event.correlation_id == "c-2"
    assert event.at <= datetime.now(UTC)


# --- one live operation per client / loan -------------------------------------------------


def test_second_disbursement_reference_for_the_same_loan_is_refused(eng: Engine) -> None:
    new(start(eng, reference="FIN-1"))
    second = start(eng, reference="FIN-2")
    assert second == Taken(ErrorCode.LOAN_ALREADY_DISBURSED, "FINERACT_PENDING")
    with eng.connect() as conn:
        assert conn.execute(select(func.count()).select_from(operations)).scalar() == 1


def test_disbursement_reference_is_refused_after_the_loan_was_disbursed(eng: Engine) -> None:
    finish(eng, new(start(eng, reference="FIN-1")), outbox_entry={"l": 1})
    assert start(eng, reference="FIN-2") == Taken(ErrorCode.LOAN_ALREADY_DISBURSED, "ERP_PENDING")


def test_a_failed_disbursement_does_not_block_another_reference(eng: Engine) -> None:
    mark_failed(eng, new(start(eng, reference="FIN-1")).id, "refused")
    second = new(start(eng, reference="FIN-2"))
    assert second.attempts == 1


def test_retrying_a_failed_disbursement_is_refused_when_another_is_live(eng: Engine) -> None:
    first = new(start(eng, reference="FIN-1"))
    mark_failed(eng, first.id, "refused")
    new(start(eng, reference="FIN-2"))
    assert start(eng, reference="FIN-1") == Taken(
        ErrorCode.LOAN_ALREADY_DISBURSED, "FINERACT_PENDING"
    )
    with eng.connect() as conn:
        status = conn.execute(select(operations.c.status).where(operations.c.id == first.id))
        assert status.scalar() == "FAILED"


def test_second_approval_reference_for_the_same_loan_is_refused(eng: Engine) -> None:
    new(start(eng, op_type="APR", reference="APR-1"))
    assert start(eng, op_type="APR", reference="APR-2") == Taken(
        ErrorCode.LOAN_ALREADY_APPROVED, "FINERACT_PENDING"
    )


def test_client_uid_reused_by_another_reference_is_taken(eng: Engine) -> None:
    payload = {"client_uid": "SME-000001"}
    new(start(eng, op_type="CLI", reference="CLI-1", payload=payload, client_uid="SME-000001"))
    second = start(eng, op_type="CLI", reference="CLI-2", payload=payload, client_uid="SME-000001")
    assert second == Taken(ErrorCode.CLIENT_UID_TAKEN, "FINERACT_PENDING")


def test_a_loan_takes_many_repayments(eng: Engine) -> None:
    for i in range(3):
        new(start(eng, op_type="RPY", reference=f"RPY-{i}"))


def test_the_database_requires_a_loan_for_loan_operations(eng: Engine) -> None:
    with pytest.raises(IntegrityError):
        start(eng, op_type="DSB", reference="NO-LOAN", loan_reference=None)


# --- many requests at once ------------------------------------------------------------------


def test_ten_identical_requests_at_once_run_once(eng: Engine) -> None:
    results = at_once(10, lambda _: start(eng))
    assert sum(isinstance(r, New) for r in results) == 1
    assert all(isinstance(r, New | InProgress | Replay) for r in results)
    with eng.connect() as conn:
        assert conn.execute(select(func.count()).select_from(operations)).scalar() == 1


def test_ten_requests_with_one_reference_and_different_data_run_once(eng: Engine) -> None:
    results = at_once(10, lambda i: start(eng, payload={**PAYLOAD, "amount": 1000 + i}))
    assert sum(isinstance(r, New) for r in results) == 1
    assert sum(isinstance(r, Conflict) for r in results) == 9


def test_ten_disbursement_references_for_one_loan_at_once_run_once(eng: Engine) -> None:
    results = at_once(10, lambda i: start(eng, reference=f"FIN-{i}"))
    assert sum(isinstance(r, New) for r in results) == 1
    taken = [r for r in results if isinstance(r, Taken)]
    assert len(taken) == 9
    assert {t.code for t in taken} == {ErrorCode.LOAN_ALREADY_DISBURSED}


def test_ten_retries_of_one_failed_request_at_once_run_once(eng: Engine) -> None:
    mark_failed(eng, new(start(eng)).id, "not sent")
    results = at_once(10, lambda _: start(eng))
    winners = [r for r in results if isinstance(r, New)]
    assert len(winners) == 1
    assert winners[0].operation.attempts == 2
    assert all(isinstance(r, New | InProgress) for r in results)


# --- loan references ------------------------------------------------------------------------


def test_loan_references_come_from_the_sequence_with_the_database_year(eng: Engine) -> None:
    with eng.begin() as conn:
        year = conn.execute(text("SELECT extract(year FROM current_date)::int")).scalar()
        conn.execute(text("SELECT setval('loan_reference_seq', 41)"))
        first = next_loan_reference(conn)
        second = next_loan_reference(conn)
    assert first == f"LN-{year}-000042"
    assert second == f"LN-{year}-000043"


# --- GET /api/transactions data ------------------------------------------------------------


def test_list_for_reference_shows_every_type_with_history_for_that_partner(eng: Engine) -> None:
    dsb = new(start(eng, reference="SHARED-1"))
    new(start(eng, op_type="RPY", reference="SHARED-1"))
    finish(eng, dsb, outbox_entry={"l": 1})
    views = list_for_reference(eng, "SHARED-1", "partner-a")
    assert [v.op_key for v in views] == ["DSB:SHARED-1", "RPY:SHARED-1"]
    assert views[0].status == "ERP_PENDING"
    assert views[0].erp_status == "PENDING"
    assert [(e.from_status, e.to_status) for e in views[0].history] == [
        (None, "RECEIVED"),
        ("RECEIVED", "FINERACT_PENDING"),
        ("FINERACT_PENDING", "FINERACT_DONE"),
        ("FINERACT_DONE", "ERP_PENDING"),
    ]
    assert list_for_reference(eng, "SHARED-1", "partner-b") == []
    assert list_for_reference(eng, "NOPE", "partner-a") == []


# --- per-loan lock ----------------------------------------------------------------------------


def test_lock_key_is_stable_and_signed_64_bit() -> None:
    key = loan_lock_key(LOAN)
    assert key == loan_lock_key(LOAN)
    assert key != loan_lock_key("LN-2026-000002")
    assert -(2**63) <= key < 2**63


def test_second_writer_to_the_same_loan_waits(eng: Engine) -> None:
    events: list[str] = []
    first_in = threading.Event()

    def first() -> None:
        with loan_lock(eng, LOAN):
            events.append("first in")
            first_in.set()
            time.sleep(0.5)
            events.append("first out")

    def second() -> None:
        first_in.wait()
        with loan_lock(eng, LOAN, timeout_seconds=5):
            events.append("second in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert events == ["first in", "first out", "second in"]


def test_other_loans_do_not_wait(eng: Engine) -> None:
    with loan_lock(eng, LOAN), loan_lock(eng, "LN-2026-000002", timeout_seconds=0.2):
        pass


def test_lock_wait_has_a_timeout(eng: Engine) -> None:
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with loan_lock(eng, LOAN):
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    holding.wait()
    started = time.monotonic()
    try:
        with pytest.raises(LoanLockTimeoutError), loan_lock(eng, LOAN, timeout_seconds=0.3):
            pass
    finally:
        release.set()
        holder.join()
    assert time.monotonic() - started < 3


def test_lock_is_released_when_the_work_fails(eng: Engine) -> None:
    with pytest.raises(RuntimeError), loan_lock(eng, LOAN):
        raise RuntimeError("Fineract call blew up")
    with loan_lock(eng, LOAN, timeout_seconds=0.2):
        pass
    # Only this loan's key in this database: other suites may hold their own locks on the
    # same Postgres server at the same time. A bigint key is split over classid and objid.
    key = loan_lock_key(LOAN) & 0xFFFFFFFFFFFFFFFF
    with eng.connect() as conn:
        held = conn.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND objsubid = 1 AND classid::bigint = :high AND objid::bigint = :low "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            ),
            {"high": key >> 32, "low": key & 0xFFFFFFFF},
        ).scalar()
    assert held == 0
