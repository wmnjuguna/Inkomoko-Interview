"""Recovery: finish what a crash or an unclear Fineract answer left behind, then send the
outbox. Runs at start-up, on every tick of the background loop, and on POST /admin/recover.

An operation still in FINERACT_PENDING is picked up when it is older than
stale_after_seconds (its own request has surely given up by then) or when its
next_attempt_at has come. It is claimed with FOR UPDATE SKIP LOCKED and a lease, so two
workers never look up the same one at once. Approvals, disbursements and repayments are
checked under the same per-loan advisory lock the routes hold around their Fineract call,
so recovery never races a live request for that loan; a busy loan is skipped until the
next pass.

Each operation type has a recoverer. It looks the operation up by our externalId (never
sends it again) and answers:
- "done": Fineract has it, and it was finished exactly as the normal path would. For a
  disbursement or a repayment the journal entry is built from the FOUND Fineract
  transaction and the outbox row is written in the same transaction as FINERACT_DONE
  (outbox.record_money_done). Every type uses the routes' own recover_* function, so the
  saved answer is the one the route would have saved.
- "failed": Fineract has no record of it, so nothing happened. FAILED; the partner may send
  the same reference again.
- "unknown": the lookup failed. recovery_lookups + 1 and a later next_attempt_at.
  NEEDS_RECONCILIATION after max_attempts failed lookups. attempts (the Idempotency-Key
  attempt) is left alone: recovery never sends anything."""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import Engine, func, select, update

from integration_service.clients.erp import ErpClient
from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.logs import log_event
from integration_service.mapping.journal import GlRoleAccounts, load_gl_role_accounts
from integration_service.models import operations
from integration_service.services.clients import recover_client_creation
from integration_service.services.loan_locks import LoanLockTimeoutError, loan_lock
from integration_service.services.loans import (
    FineractApi,
    recover_application,
    recover_approval,
)
from integration_service.services.money import recover_money_operation
from integration_service.services.operations import (
    Operation,
    StaleStatusError,
    get_operation,
    move,
)
from integration_service.services.outbox import (
    DEFAULT_MAX_ATTEMPTS,
    JournalPoster,
    SendReport,
    backoff_seconds,
    send_due,
)
from integration_service.state_machine import FINERACT_PENDING, NEEDS_RECONCILIATION

logger = logging.getLogger("integration_service.recovery")

# Older than this, a FINERACT_PENDING request has given up: its lock wait, Fineract call
# and lookup are each bounded by a 30 s timeout by default.
DEFAULT_STALE_AFTER_SECONDS = 180.0
DEFAULT_LOCK_WAIT_SECONDS = 2.0
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_BATCH_SIZE = 20
LOOKUP_FAILED = "recovery: lookup failed; will look again before any retry"
# Types whose Fineract write the routes make under the per-loan lock.
_LOCKED_TYPES = frozenset({"APR", "DSB", "RPY"})

Outcome = Literal["done", "failed", "unknown"]
Accounts = Callable[[], GlRoleAccounts]
Recoverer = Callable[[Engine, FineractApi, Operation, Accounts], str]


@dataclass
class OperationsReport:
    checked: int = 0
    completed: int = 0
    needs_reconciliation: int = 0
    failed: int = 0
    still_unknown: int = 0
    skipped_busy: int = 0
    finished_meanwhile: int = 0


@dataclass
class RecoveryReport:
    """Counts only: no ids. This is what POST /admin/recover shows."""

    operations: OperationsReport = field(default_factory=OperationsReport)
    outbox: SendReport = field(default_factory=SendReport)

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {"operations": asdict(self.operations), "outbox": asdict(self.outbox)}


# --- the pass ----------------------------------------------------------------------------------


def recover_once(
    engine: Engine,
    fineract: FineractApi,
    erp: JournalPoster,
    *,
    accounts: Accounts,
    now: datetime | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    send_outbox: bool = True,
    recoverers: Mapping[str, Recoverer] | None = None,
    rand: Callable[[], float] = random.random,
) -> RecoveryReport:
    """One pass: unfinished operations first, then the due outbox rows."""
    now = now or datetime.now(UTC)
    chosen = {**RECOVERERS, **(recoverers or {})}
    report = RecoveryReport()
    for op in _claim(engine, now, stale_after_seconds, batch_size):
        report.operations.checked += 1
        try:
            if op.op_type in _LOCKED_TYPES and op.loan_reference is not None:
                with loan_lock(engine, op.loan_reference, lock_wait_seconds):
                    result = _recover(engine, fineract, chosen[op.op_type], op, accounts)
            else:
                result = _recover(engine, fineract, chosen[op.op_type], op, accounts)
        except LoanLockTimeoutError:
            _log(op, "recovery skipped: the loan is being written", FINERACT_PENDING)
            _release_lease(engine, op)
            result = "skipped_busy"
        except StaleStatusError:
            _log(op, "recovery skipped: the request finished meanwhile", None)
            result = "finished_meanwhile"
        if result == "unknown":
            result = _still_unknown(engine, op, now, max_attempts, rand)
        setattr(report.operations, result, getattr(report.operations, result) + 1)
    if send_outbox:
        # Rows written during this pass are stamped with the database's clock, after `now`.
        send_at = max(now, datetime.now(UTC))
        report.outbox = send_due(engine, erp, send_at, max_attempts=max_attempts, rand=rand)
    return report


def _claim(
    engine: Engine, now: datetime, stale_after_seconds: float, batch_size: int
) -> list[Operation]:
    due_at = func.coalesce(
        operations.c.next_attempt_at,
        operations.c.updated_at + timedelta(seconds=stale_after_seconds),
    )
    with engine.begin() as conn:
        rows = (
            conn.execute(
                select(operations)
                .where(operations.c.status == FINERACT_PENDING)
                .where(due_at <= now)
                .order_by(operations.c.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            .mappings()
            .all()
        )
        for row in rows:
            # A lease: hidden from other passes while this one works on it.
            conn.execute(
                update(operations)
                .where(operations.c.id == row["id"])
                .values(next_attempt_at=now + timedelta(seconds=DEFAULT_LEASE_SECONDS))
            )
    return [Operation.from_row(row) for row in rows]


def _recover(
    engine: Engine,
    fineract: FineractApi,
    recoverer: Recoverer,
    op: Operation,
    accounts: Accounts,
) -> str:
    """Runs the recoverer and names the result as a report field ("unknown" aside)."""
    current = get_operation(engine, op.op_type, op.external_reference)
    if current is None or current.status != FINERACT_PENDING:
        return "finished_meanwhile"
    outcome = recoverer(engine, fineract, current, accounts)
    if outcome in ("unknown", "failed"):
        return outcome
    done = get_operation(engine, op.op_type, op.external_reference) or current
    _log(done, "recovery: found in Fineract and finished", done.status)
    return "needs_reconciliation" if done.status == NEEDS_RECONCILIATION else "completed"


def _still_unknown(
    engine: Engine,
    op: Operation,
    now: datetime,
    max_attempts: int,
    rand: Callable[[], float],
) -> str:
    # Counted apart from attempts, which is the Idempotency-Key attempt of the write.
    lookups = op.recovery_lookups + 1
    if lookups >= max_attempts:
        move(
            engine,
            op.id,
            NEEDS_RECONCILIATION,
            f"recovery: Fineract lookup failed {lookups} times",
            op.correlation_id,
            expected=FINERACT_PENDING,
            recovery_lookups=lookups,
            last_error=LOOKUP_FAILED,
        )
        return "needs_reconciliation"
    with engine.begin() as conn:
        conn.execute(
            update(operations)
            .where(operations.c.id == op.id)
            .where(operations.c.status == FINERACT_PENDING)
            .values(
                recovery_lookups=lookups,
                last_error=LOOKUP_FAILED,
                next_attempt_at=now + timedelta(seconds=backoff_seconds(lookups, rand())),
                updated_at=datetime.now(UTC),
            )
        )
    _log(op, LOOKUP_FAILED, FINERACT_PENDING, logging.WARNING)
    return "still_unknown"


def _release_lease(engine: Engine, op: Operation) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(operations)
            .where(operations.c.id == op.id)
            .where(operations.c.status == FINERACT_PENDING)
            .values(next_attempt_at=op.next_attempt_at)
        )


def _log(op: Operation, message: str, status: str | None, level: int = logging.INFO) -> None:
    log_event(
        message,
        level=level,
        correlation_id=op.correlation_id,
        op_key=op.op_key,
        external_reference=op.external_reference,
        client_uid=op.client_uid,
        loan_reference=op.loan_reference,
        fineract_transaction_id=op.fineract_transaction_id,
        erp_transaction_id=op.erp_transaction_id,
        status=status,
    )


# --- the recoverer for each type ------------------------------------------------------------


def _routes(
    fn: Callable[[Engine, FineractApi, Operation, str | None], str],
) -> Recoverer:
    """A route's own recover_* function, which needs no GL accounts."""

    def run(engine: Engine, fineract: FineractApi, op: Operation, accounts: Accounts) -> str:
        return fn(engine, fineract, op, op.correlation_id)

    return run


RECOVERERS: dict[str, Recoverer] = {
    "CLI": _routes(recover_client_creation),
    "APP": _routes(recover_application),
    "APR": _routes(recover_approval),
    # The routes' own recoverer: the saved answer has the same shape as the route's.
    "DSB": recover_money_operation,
    "RPY": recover_money_operation,
}


# --- the background loop -----------------------------------------------------------------------


class BackgroundLoop:
    """Runs tick() at start and then every interval_seconds on a daemon thread, until
    stop(). An exception in tick() is logged and the loop goes on."""

    def __init__(self, tick: Callable[[], object], interval_seconds: float, name: str) -> None:
        self._tick = tick
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("background loop tick failed")
                log_event("background loop tick failed", level=logging.ERROR)
            self._stop.wait(self._interval)


# --- wiring from Settings ----------------------------------------------------------------------


def gl_role_accounts_from(path: str) -> Accounts:
    """Reads the GL role accounts once, on first use, and keeps them. A failed read is not
    kept, so fixing the file is enough."""
    cache: list[GlRoleAccounts] = []
    guard = threading.Lock()

    def load() -> GlRoleAccounts:
        with guard:
            if not cache:
                cache.append(load_gl_role_accounts(path))
            return cache[0]

    return load


def recover_with_settings(settings: Settings, engine: Engine, accounts: Accounts) -> RecoveryReport:
    """One pass with fresh clients made from the settings."""
    erp = ErpClient(settings.erp_base_url, settings.erp_api_key, settings.erp_timeout_seconds)
    fineract = FineractClient(
        base_url=settings.fineract_base_url,
        tenant=settings.fineract_tenant,
        username=settings.fineract_username,
        password=settings.fineract_password,
        timeout_seconds=settings.fineract_timeout_seconds,
    )
    try:
        return recover_once(
            engine,
            fineract,
            erp,
            accounts=accounts,
            max_attempts=settings.outbox_max_attempts,
            stale_after_seconds=max(
                DEFAULT_STALE_AFTER_SECONDS, 3 * settings.fineract_timeout_seconds + 30
            ),
        )
    finally:
        erp.close()
        fineract.close()


def start_background_loop(settings: Settings, engine: Engine, accounts: Accounts) -> BackgroundLoop:
    """One loop per process (each gunicorn worker runs the app factory once). Its first
    tick is the start-up recovery."""

    def tick() -> None:
        report = recover_with_settings(settings, engine, accounts)
        if report.operations.checked or report.outbox.claimed:
            counts = report.as_dict()
            log_event("recovery pass", operations=counts["operations"], outbox=counts["outbox"])

    loop = BackgroundLoop(tick, settings.outbox_poll_seconds, "outbox-recovery")
    loop.start()
    return loop
