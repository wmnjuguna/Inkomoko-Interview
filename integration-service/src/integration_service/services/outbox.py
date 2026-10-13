"""The outbox: journal entries waiting for the ERP.

An outbox row is written in the same database transaction that records the Fineract result
(record_money_done), so a crash can never lose an entry or send one for a Fineract
transaction that was not recorded.

send_due() delivers due rows:
1. One short transaction claims them with SELECT ... FOR UPDATE SKIP LOCKED, adds 1 to
   attempts and pushes next_attempt_at forward (a lease), so another sender, thread or
   worker skips them while the call runs.
2. The ERP call happens outside any transaction.
3. A second short transaction records the result, only if the row is still the one we
   claimed (same attempts, still PENDING).

The ERP is idempotent by external_reference: a repeat of the same entry gets 200 with the
first entry. So resending after a timeout is always safe, and a crash between the ERP call
and step 3 only means one more (harmless) send.

What each answer leads to:
- 201 or 200: SENT. The operation moves ERP_PENDING -> COMPLETED with erp_status SENT.
- Not reachable, timeout, 5xx: try again after 1, 2, 4, ... 60 s plus up to 25% jitter.
  After max_attempts: NEEDS_RECONCILIATION.
- 429: wait as long as Retry-After says. It does not count as an attempt: the ERP asked us
  to slow down, it did not fail.
- 409 (the ERP has another entry with this reference) or another refusal (422):
  NEEDS_RECONCILIATION at once. Sending it again would get the same answer.
- 401 or 403: a wrong ERP key. Retried like an outage, so fixing the key is enough.
Every outcome is written to operation_events and logged."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, RowMapping, insert, select, update

from integration_service.clients.errors import (
    ErpConflict,
    ErpError,
    ErpRateLimited,
    ErpRejected,
)
from integration_service.logs import log_event
from integration_service.mapping.journal import JournalPlan
from integration_service.models import operation_events, operations, outbox
from integration_service.services.operations import Operation, mark_fineract_done, move
from integration_service.state_machine import COMPLETED, ERP_PENDING, NEEDS_RECONCILIATION

MAX_BACKOFF_SECONDS = 60.0
JITTER = 0.25
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BATCH_SIZE = 20
# How long a claimed row is hidden from other senders while its ERP call runs. Longer
# than the ERP client timeout. A batch is sent one row at a time, so the lease is renewed
# just before each send: a row whose lease ran out while earlier rows were slow, and that
# another sender has claimed since, is skipped, never sent by both.
DEFAULT_LEASE_SECONDS = 120.0
_RETRYABLE_REFUSALS = frozenset({401, 403})


@dataclass(frozen=True)
class RetryAt:
    at: datetime
    reason: str
    counts: bool = True


@dataclass(frozen=True)
class Reconcile:
    reason: str


def backoff_seconds(attempt: int, rand: float) -> float:
    """Wait after failed attempt number `attempt` (1, 2, 4, ... 60), plus up to 25% jitter.
    rand is in [0, 1)."""
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    base = min(MAX_BACKOFF_SECONDS, 2.0 ** min(attempt - 1, 16))
    return min(MAX_BACKOFF_SECONDS, base * (1 + JITTER * rand))


def after_failure(
    exc: ErpError, attempts: int, max_attempts: int, now: datetime, rand: float
) -> RetryAt | Reconcile:
    """What a failed send leads to. attempts counts this one."""
    if isinstance(exc, ErpConflict):
        return Reconcile("the ERP holds this reference with different content (409)")
    if isinstance(exc, ErpRejected) and exc.status not in _RETRYABLE_REFUSALS:
        return Reconcile(f"the ERP refused the entry ({exc.code or exc.status})")
    if isinstance(exc, ErpRateLimited):
        wait = exc.retry_after if exc.retry_after is not None else backoff_seconds(attempts, rand)
        return RetryAt(now + timedelta(seconds=wait), f"ERP rate limit, wait {wait} s", False)
    what = _describe(exc)
    if attempts >= max_attempts:
        return Reconcile(f"{what}; gave up after {attempts} attempts")
    wait = backoff_seconds(attempts, rand)
    return RetryAt(now + timedelta(seconds=wait), f"{what}; retry in {wait:.1f} s")


def _describe(exc: ErpError) -> str:
    if isinstance(exc, ErpRejected):
        return f"the ERP refused our key ({exc.status})"
    return {
        "ErpUnavailable": "ERP not reachable",
        "ErpTimeout": "no answer from the ERP",
        "ErpServerError": "ERP server error",
    }.get(type(exc).__name__, "ERP call failed")


# --- writing the outbox row ------------------------------------------------------------------


def record_money_done(
    conn: Connection,
    operation_id: int,
    plan: JournalPlan,
    *,
    response_status: int,
    response_body: Any,
    fineract_resource_id: int | None = None,
    fineract_transaction_id: int | None = None,
    detail: str = "Fineract said yes",
    correlation_id: str | None = None,
) -> Operation:
    """In the caller's transaction: the operation is FINERACT_DONE with its saved answer and
    the outbox row holds its journal entry (ERP_PENDING). When the entry could not be built
    (plan.problem), the row is kept for the record and both go to NEEDS_RECONCILIATION:
    nothing is guessed and nothing is sent. The disbursement and repayment routes and
    recovery all finish a money operation through this one function."""
    operation = mark_fineract_done(
        conn,
        operation_id,
        response_status=response_status,
        response_body=response_body,
        fineract_resource_id=fineract_resource_id,
        fineract_transaction_id=fineract_transaction_id,
        outbox_entry=plan.outbox_entry(),
        detail=detail,
        correlation_id=correlation_id,
    )
    if plan.entry is not None:
        return operation
    reason = f"journal entry not built: {plan.problem}"
    conn.execute(
        update(outbox)
        .where(outbox.c.operation_id == operation_id)
        .values(status=NEEDS_RECONCILIATION, last_error=reason, updated_at=datetime.now(UTC))
    )
    return move(
        conn,
        operation_id,
        NEEDS_RECONCILIATION,
        reason,
        correlation_id,
        erp_status=NEEDS_RECONCILIATION,
        last_error=reason,
    )


# --- sending ---------------------------------------------------------------------------------


class JournalPoster(Protocol):
    def post_journal_entry(
        self, entry: dict[str, Any], correlation_id: str | None = None
    ) -> dict[str, Any]: ...


@dataclass
class SendReport:
    """Counts only: safe to show on /admin/recover."""

    claimed: int = 0
    sent: int = 0
    retried: int = 0
    rate_limited: int = 0
    needs_reconciliation: int = 0
    stale: int = 0

    def add(self, other: SendReport) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass(frozen=True)
class _Claimed:
    outbox_id: int
    operation_id: int
    attempts: int
    entry: dict[str, Any]
    op_key: str
    external_reference: str
    client_uid: str | None
    loan_reference: str | None
    fineract_transaction_id: int | None
    correlation_id: str | None


@dataclass(frozen=True)
class _Sent:
    erp_transaction_id: str | None


_Result = _Sent | RetryAt | Reconcile


def send_due(
    engine: Engine,
    erp: JournalPoster,
    now: datetime | None = None,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    rand: Callable[[], float] = random.random,
) -> SendReport:
    """Send the PENDING rows that are due at `now`, at most batch_size of them."""
    now = now or datetime.now(UTC)
    report = SendReport()
    claimed = _claim(engine, now, batch_size, lease_seconds)
    report.claimed = len(claimed)
    for row in claimed:
        if not _renew_lease(engine, row, now, lease_seconds):
            _log(row, "outbox row skipped: another sender claimed it", "STALE", None)
            report.stale += 1
            continue
        result = _send(erp, row, max_attempts, now, rand)
        recorded = _record(engine, row, result)
        if not recorded:
            report.stale += 1
        elif isinstance(result, _Sent):
            report.sent += 1
        elif isinstance(result, Reconcile):
            report.needs_reconciliation += 1
        elif result.counts:
            report.retried += 1
        else:
            report.rate_limited += 1
    return report


def _claim(engine: Engine, now: datetime, batch_size: int, lease_seconds: float) -> list[_Claimed]:
    with engine.begin() as conn:
        rows = (
            conn.execute(
                select(
                    outbox.c.id,
                    outbox.c.operation_id,
                    outbox.c.attempts,
                    outbox.c.entry,
                    operations.c.op_key,
                    operations.c.external_reference,
                    operations.c.client_uid,
                    operations.c.loan_reference,
                    operations.c.fineract_transaction_id,
                    operations.c.correlation_id,
                )
                .join(operations, operations.c.id == outbox.c.operation_id)
                .where(outbox.c.status == "PENDING")
                .where(outbox.c.next_attempt_at <= now)
                .order_by(outbox.c.next_attempt_at, outbox.c.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True, of=outbox)
            )
            .mappings()
            .all()
        )
        claimed = [_claimed(row) for row in rows]
        for row in claimed:
            conn.execute(
                update(outbox)
                .where(outbox.c.id == row.outbox_id)
                .values(
                    attempts=row.attempts,
                    next_attempt_at=now + timedelta(seconds=lease_seconds),
                    updated_at=datetime.now(UTC),
                )
            )
    return claimed


def _renew_lease(engine: Engine, row: _Claimed, now: datetime, lease_seconds: float) -> bool:
    """Just before the send: extend the lease if the row is still ours (same attempts, still
    PENDING). False when another sender claimed it after our lease ran out."""
    start = max(now, datetime.now(UTC))
    with engine.begin() as conn:
        mine = conn.execute(
            update(outbox)
            .where(outbox.c.id == row.outbox_id)
            .where(outbox.c.status == "PENDING")
            .where(outbox.c.attempts == row.attempts)
            .values(next_attempt_at=start + timedelta(seconds=lease_seconds))
            .returning(outbox.c.id)
        ).one_or_none()
    return mine is not None


def _claimed(row: RowMapping) -> _Claimed:
    return _Claimed(
        outbox_id=row["id"],
        operation_id=row["operation_id"],
        attempts=row["attempts"] + 1,
        entry=row["entry"],
        op_key=row["op_key"],
        external_reference=row["external_reference"],
        client_uid=row["client_uid"],
        loan_reference=row["loan_reference"],
        fineract_transaction_id=row["fineract_transaction_id"],
        correlation_id=row["correlation_id"],
    )


def _send(
    erp: JournalPoster,
    row: _Claimed,
    max_attempts: int,
    now: datetime,
    rand: Callable[[], float],
) -> _Result:
    try:
        answer = erp.post_journal_entry(row.entry, correlation_id=row.correlation_id)
    except ErpError as exc:
        return after_failure(exc, row.attempts, max_attempts, now, rand())
    erp_id = answer.get("erp_transaction_id")
    return _Sent(str(erp_id) if erp_id is not None else None)


def _record(engine: Engine, row: _Claimed, result: _Result) -> bool:
    """Write the result if the row is still ours. False when another sender claimed it."""
    stamp = datetime.now(UTC)
    if isinstance(result, _Sent):
        values: dict[str, Any] = {
            "status": "SENT",
            "erp_transaction_id": result.erp_transaction_id,
            "last_error": None,
        }
        target, detail = COMPLETED, "the ERP saved the journal entry"
    elif isinstance(result, Reconcile):
        values = {"status": NEEDS_RECONCILIATION, "last_error": result.reason}
        target, detail = NEEDS_RECONCILIATION, result.reason
    else:
        attempts = row.attempts if result.counts else row.attempts - 1
        values = {"next_attempt_at": result.at, "last_error": result.reason, "attempts": attempts}
        target, detail = ERP_PENDING, result.reason

    with engine.begin() as conn:
        mine = conn.execute(
            update(outbox)
            .where(outbox.c.id == row.outbox_id)
            .where(outbox.c.status == "PENDING")
            .where(outbox.c.attempts == row.attempts)
            .values(updated_at=stamp, **values)
            .returning(outbox.c.id)
        ).one_or_none()
        if mine is None:
            _log(row, "outbox result dropped: the row was claimed again", "STALE", None)
            return False
        current = conn.execute(
            select(operations.c.status).where(operations.c.id == row.operation_id).with_for_update()
        ).scalar_one()
        erp_id = result.erp_transaction_id if isinstance(result, _Sent) else None
        if current != ERP_PENDING:
            # Repaired by hand meanwhile: keep the outbox result, leave the status alone.
            _log(row, f"outbox result kept; operation is {current}", current, erp_id)
        elif target == ERP_PENDING:
            conn.execute(
                insert(operation_events).values(
                    operation_id=row.operation_id,
                    from_status=ERP_PENDING,
                    to_status=ERP_PENDING,
                    detail=detail,
                    correlation_id=row.correlation_id,
                )
            )
            _log(row, f"outbox send failed: {detail}", ERP_PENDING, None, logging.WARNING)
        else:
            erp_status = "SENT" if target == COMPLETED else NEEDS_RECONCILIATION
            extra: dict[str, Any] = {}
            if erp_id is not None:
                extra["erp_transaction_id"] = erp_id
            if target == NEEDS_RECONCILIATION:
                extra["last_error"] = detail
            move(
                conn,
                row.operation_id,
                target,
                detail,
                row.correlation_id,
                expected=ERP_PENDING,
                erp_status=erp_status,
                **extra,
            )
            level = logging.INFO if target == COMPLETED else logging.WARNING
            _log(row, f"outbox {erp_status.lower()}: {detail}", target, erp_id, level)
    return True


def _log(
    row: _Claimed,
    message: str,
    status: str,
    erp_transaction_id: str | None,
    level: int = logging.INFO,
) -> None:
    log_event(
        message,
        level=level,
        correlation_id=row.correlation_id,
        op_key=row.op_key,
        external_reference=row.external_reference,
        client_uid=row.client_uid,
        loan_reference=row.loan_reference,
        fineract_transaction_id=row.fineract_transaction_id,
        erp_transaction_id=erp_transaction_id,
        status=status,
        attempts=row.attempts,
    )
