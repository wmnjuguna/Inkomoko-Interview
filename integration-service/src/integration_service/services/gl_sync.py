"""GL sync: copy the ERP chart of accounts into Fineract, through the plugin.

Why an explicit call (POST /api/accounts/sync) and not a timer or an ERP event:
- The sync only creates accounts. Changing or closing an account is out of scope, so
  there is nothing to keep polling for.
- Each run does the same thing in the same order, so its result is easy to check.
- Order matters: parents before children, and the whole chart before the loan product
  is set up, because the product maps its GL roles to these accounts.

One run:
1. Read the ERP chart. Put parents before children.
2. Record each account in gl_sync with a hash of the body we would send. A new account is
   SYNC_PENDING. If the ERP now sends different data for an account we already know:
   - not SYNCED yet: we take the new data. Fineract never accepted the old data, so there
     is nothing to update. (If an earlier send had no clear answer, the lookup in step 3
     still comes first, and Fineract holding the old data makes it FAILED.)
   - SYNCED: it stays SYNCED, Fineract is not called, and last_error says CHANGED_IN_ERP.
     Updates are out of scope. Children and the loan product can still use it.
3. For each account that is not SYNCED, parents first:
   - a child whose parent is not SYNCED is skipped (FAILED, or waiting while the parent
     is SYNC_PENDING);
   - an account sent before without a clear answer is looked up first (GET on the
     plugin). It is never sent again blindly;
   - otherwise PUT on the plugin with Idempotency-Key GLS:<reference>:<attempt>:<hash>.
     200 -> SYNCED. 409 or another 4xx -> FAILED with the reason. Not reachable ->
     SYNC_PENDING, and the run stops calling Fineract. Timeout or 5xx -> look it up at once;
     found with the same GL code -> SYNCED, otherwise SYNC_PENDING.
4. Answer with the state of every account.

Retry when Fineract is down: the report shows SYNC_PENDING or FAILED, and calling the
sync again later finishes the job. There is no background loop yet.

Only one run at a time: a Postgres advisory lock covers the whole run. A second caller does
not wait and calls nothing; it gets the current report with in_progress=True.

A new key per attempt: if a crash happens after the PUT but before we save the attempt,
the next run sends the same key again and Fineract replays its first answer. A later attempt
gets a new key, so an old cached failure is never replayed. The key also ends with the start
of the payload hash, so other data never gets the cached answer for old data (for example
if integration_db is rebuilt while Fineract keeps its cache). The plugin is idempotent on
the reference anyway: the same data again answers UNCHANGED.

last_error is shown to partners, so it only holds our own words and Fineract's error codes,
never Fineract's text or body (they can hold Fineract ids)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, RowMapping, insert, select, text, update

from integration_service.clients.erp import ErpAccount
from integration_service.clients.errors import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    FineractError,
    FineractRejected,
    FineractServerError,
    FineractUnavailable,
)
from integration_service.clients.fineract import ErpAccountMapping, ErpAccountSyncResult
from integration_service.hashing import request_hash
from integration_service.logs import log_event
from integration_service.mapping.account_types import build_fineract_gl_body
from integration_service.models import GL_SYNC_STATUSES, gl_sync

# Any fixed number works, as long as nothing else in this database uses it.
GL_SYNC_LOCK_KEY = 0x474C5F53594E43  # "GL_SYNC"

SYNCED = "SYNCED"
SYNC_PENDING = "SYNC_PENDING"
FAILED = "FAILED"
CHANGED_IN_ERP = "changed in ERP; updates are out of scope"
OLDER_DATA_IN_FINERACT = "Fineract holds {reference} with older data; updates are out of scope"
KEY_HASH_LENGTH = 8

_CONFLICT_REASONS = {
    "error.msg.glaccount.sync.code.conflict": "GL_CODE_TAKEN: GL code {code} is already used "
    "by another GL account in Fineract",
    "error.msg.glaccount.sync.payload.conflict": "Fineract already holds {reference} with "
    "different data",
    "error.msg.glaccount.sync.parent.not.synced": "Fineract says parent {parent} is not synced",
    "error.msg.glaccount.sync.parent.not.header": "Fineract says parent {parent} is not a "
    "HEADER account",
    "error.msg.glaccount.sync.parent.type.mismatch": "Fineract says parent {parent} has "
    "another type",
}


class SyncInProgressError(RuntimeError):
    """Another GL sync holds the run lock. Nothing was called."""


class ErpChart(Protocol):
    def list_accounts(self, correlation_id: str | None = None) -> list[ErpAccount]: ...


class FineractGl(Protocol):
    def put_erp_account(
        self,
        erp_reference: str,
        body: dict[str, Any],
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> ErpAccountSyncResult: ...

    def get_erp_account(
        self, erp_reference: str, correlation_id: str | None = None
    ) -> ErpAccountMapping | None: ...


@dataclass(frozen=True)
class AccountStatus:
    """One account in the report. It never holds Fineract's ids."""

    erp_reference: str
    code: str
    name: str
    status: str
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class SyncReport:
    accounts: list[AccountStatus]
    writes: int = 0
    in_progress: bool = False

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(GL_SYNC_STATUSES, 0)
        for account in self.accounts:
            counts[account.status] += 1
        return counts


def idempotency_key(erp_reference: str, attempt: int, payload_hash: str) -> str:
    """GLS:<reference>:<attempt>:<hash start>. A reference too long for Fineract's 50
    characters is hashed."""
    data = payload_hash[:KEY_HASH_LENGTH]
    key = f"GLS:{erp_reference}:{attempt}:{data}"
    if len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        return key
    digest = hashlib.sha256(erp_reference.encode("utf-8")).hexdigest()[:24]
    return f"GLS:{digest}:{attempt}:{data}"


def get_report(engine: Engine) -> SyncReport:
    """The state of every account, without calling anything."""
    with engine.connect() as conn:
        rows = conn.execute(select(gl_sync).order_by(gl_sync.c.erp_code)).mappings().all()
    return SyncReport([_status(row) for row in rows])


def sync_all(
    engine: Engine, erp: ErpChart, fineract: FineractGl, correlation_id: str | None = None
) -> SyncReport:
    """Sync every ERP account that is not SYNCED yet. Safe to call again at any time."""
    with _run_lock(engine) as locked:
        if not locked:
            log_event("gl sync already running", correlation_id=correlation_id)
            return SyncReport(get_report(engine).accounts, in_progress=True)
        run = _Run(engine, fineract, correlation_id)
        run.sync(erp.list_accounts(correlation_id=correlation_id))
        report = SyncReport(get_report(engine).accounts, writes=run.writes)
    log_event("gl sync finished", correlation_id=correlation_id, writes=run.writes, **report.counts)
    return report


def sync_one(
    engine: Engine,
    erp: ErpChart,
    fineract: FineractGl,
    erp_reference: str,
    correlation_id: str | None = None,
) -> AccountStatus:
    """Sync one ERP account. Its parent must be SYNCED already. LookupError if the ERP
    has no account with this reference. SyncInProgressError if a run holds the lock."""
    with _run_lock(engine) as locked:
        if not locked:
            raise SyncInProgressError("A GL sync is already running")
        accounts = erp.list_accounts(correlation_id=correlation_id)
        wanted = [a for a in accounts if a.external_reference == erp_reference]
        if not wanted:
            raise LookupError(f"The ERP has no account {erp_reference}")
        _Run(engine, fineract, correlation_id).sync(accounts, only=erp_reference)
    with engine.connect() as conn:
        row = _row(conn, erp_reference)
    assert row is not None
    return _status(row)


@contextmanager
def _run_lock(engine: Engine) -> Iterator[bool]:
    """Hold the advisory lock on one connection for the whole run. Yields False if taken."""
    with engine.connect() as conn:
        locked = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": GL_SYNC_LOCK_KEY}).scalar()
        )
        conn.commit()
        try:
            yield locked
        finally:
            if locked:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": GL_SYNC_LOCK_KEY})
                conn.commit()


def parents_first(accounts: list[ErpAccount]) -> list[ErpAccount]:
    """Each account after its parent, otherwise in the ERP's order."""
    by_code = {a.code: a for a in accounts}
    ordered: list[ErpAccount] = []
    placed: set[str] = set()

    def place(account: ErpAccount, seen: frozenset[str]) -> None:
        if account.code in placed or account.code in seen:
            return
        parent = by_code.get(account.parent_code or "")
        if parent is not None:
            place(parent, seen | {account.code})
        ordered.append(account)
        placed.add(account.code)

    for account in accounts:
        place(account, frozenset())
    return ordered


class _Run:
    """One pass over the chart. Each account change is its own short transaction."""

    def __init__(self, engine: Engine, fineract: FineractGl, correlation_id: str | None) -> None:
        self.engine = engine
        self.fineract = fineract
        self.correlation_id = correlation_id
        self.writes = 0
        self.fineract_down = False

    def sync(self, accounts: list[ErpAccount], only: str | None = None) -> None:
        by_code = {a.code: a for a in accounts}
        for account in parents_first(accounts):
            if only is not None and account.external_reference != only:
                continue
            parent = by_code.get(account.parent_code) if account.parent_code else None
            parent_reference = parent.external_reference if parent else None
            if account.parent_code and parent is None:
                self._record_missing_parent(account)
                continue
            body = build_fineract_gl_body(account, parent_reference)
            if self._record(account, parent_reference, body):
                self._sync_account(account, parent_reference, body)

    # --- step 2: record what the ERP sent ------------------------------------------------

    def _record(self, account: ErpAccount, parent: str | None, body: dict[str, Any]) -> bool:
        """Insert or check the row. False when the account is SYNCED but the ERP changed it."""
        payload_hash = request_hash(body)
        reference = account.external_reference
        with self.engine.begin() as conn:
            row = _row(conn, account.external_reference, for_update=True)
            if row is None:
                conn.execute(
                    insert(gl_sync).values(
                        erp_reference=account.external_reference,
                        erp_code=account.code,
                        name=account.name,
                        erp_type=account.type,
                        erp_usage=account.usage,
                        erp_parent_reference=parent,
                        payload_hash=payload_hash,
                        status=SYNC_PENDING,
                    )
                )
                return True
            same = bool(row["payload_hash"] == payload_hash)
            if row["status"] == SYNCED:
                # Fineract holds what we sent. Only report a change; never send it.
                warning = None if same else CHANGED_IN_ERP
                if row["last_error"] != warning:
                    _set(conn, reference, last_error=warning)
                    self._log(reference, SYNCED, warning)
                return same
            if not same:
                # Fineract never accepted the old data, so take the ERP's current data.
                _set(
                    conn,
                    reference,
                    payload_hash=payload_hash,
                    erp_code=account.code,
                    name=account.name,
                    erp_type=account.type,
                    erp_usage=account.usage,
                    erp_parent_reference=parent,
                )
            return True

    def _record_missing_parent(self, account: ErpAccount) -> None:
        reason = f"parent code {account.parent_code} is not in the ERP chart"
        source = account.model_dump(mode="json", exclude={"created_at"})
        with self.engine.begin() as conn:
            row = _row(conn, account.external_reference, for_update=True)
            if row is None:
                conn.execute(
                    insert(gl_sync).values(
                        erp_reference=account.external_reference,
                        erp_code=account.code,
                        name=account.name,
                        erp_type=account.type,
                        erp_usage=account.usage,
                        payload_hash=request_hash(source),
                        status=FAILED,
                        last_error=reason,
                    )
                )
            elif row["status"] != SYNCED:  # a SYNCED account stays in Fineract anyway
                _set(conn, account.external_reference, status=FAILED, last_error=reason)

    # --- step 3: send to Fineract --------------------------------------------------------

    def _sync_account(self, account: ErpAccount, parent: str | None, body: dict[str, Any]) -> None:
        reference = account.external_reference
        with self.engine.connect() as conn:
            row = _row(conn, reference)
            parent_row = _row(conn, parent) if parent else None
        assert row is not None
        if row["status"] == SYNCED:
            return
        if parent is not None and (parent_row is None or parent_row["status"] != SYNCED):
            waiting = parent_row is not None and parent_row["status"] == SYNC_PENDING
            if waiting:
                self._save(reference, SYNC_PENDING, f"waiting for parent {parent}")
            else:
                self._save(reference, FAILED, f"parent {parent} not synced")
            return
        if self.fineract_down:
            if row["status"] == SYNC_PENDING:
                self._save(reference, SYNC_PENDING, "Fineract unavailable; not tried in this run")
            return

        # Sent before without a clear answer: look it up before sending again.
        if row["status"] == SYNC_PENDING and row["attempts"] > 0 and self._found(account, body):
            return

        attempt = row["attempts"] + 1
        key = idempotency_key(reference, attempt, row["payload_hash"])
        self.writes += 1
        try:
            result = self.fineract.put_erp_account(
                reference, body, idempotency_key=key, correlation_id=self.correlation_id
            )
        except FineractRejected as exc:
            reason = _rejection_reason(exc, account, parent)
            self._save(reference, FAILED, reason, attempts=attempt)
            return
        except FineractUnavailable:
            self.fineract_down = True
            self._save(reference, SYNC_PENDING, "Fineract unavailable", attempts=attempt)
            return
        except FineractError as exc:
            # Sent, but the outcome is unknown. Look it up now, never send it blindly.
            self._save(
                reference,
                SYNC_PENDING,
                f"Fineract did not answer ({_what_happened(exc)}); will check before retrying",
                attempts=attempt,
            )
            self._found(account, body)
            return
        self._save(reference, SYNCED, None, attempts=attempt, gl_id=result.gl_account_id)

    def _found(self, account: ErpAccount, body: dict[str, Any]) -> bool:
        """Look the account up in Fineract. False means it is not there, so send it.
        True means nothing more is done now: found, mapped elsewhere, or no answer."""
        reference = account.external_reference
        try:
            mapping = self.fineract.get_erp_account(reference, correlation_id=self.correlation_id)
        except FineractError as exc:
            if isinstance(exc, FineractUnavailable):
                self.fineract_down = True
            self._save(
                reference,
                SYNC_PENDING,
                f"Fineract did not answer the lookup ({_what_happened(exc)}); "
                "will check before retrying",
            )
            return True
        if mapping is None:
            return False
        if mapping.gl_code != account.code:
            reason = f"Fineract maps {reference} to GL code {mapping.gl_code}, not {account.code}"
            self._save(reference, FAILED, reason)
            return True
        if mapping.name != body["name"]:
            # An earlier send landed, then the ERP changed the account.
            self._save(reference, FAILED, OLDER_DATA_IN_FINERACT.format(reference=reference))
            return True
        self._save(reference, SYNCED, None, gl_id=mapping.gl_account_id)
        return True

    def _save(
        self,
        reference: str,
        status: str,
        last_error: str | None,
        attempts: int | None = None,
        gl_id: int | None = None,
    ) -> None:
        now = datetime.now(UTC)
        values: dict[str, Any] = {"status": status, "last_error": last_error}
        if attempts is not None:
            values.update(attempts=attempts, last_attempt_at=now)
        if status == SYNCED:
            values.update(fineract_gl_id=gl_id, synced_at=now)
        with self.engine.begin() as conn:
            _set(conn, reference, **values)
        self._log(reference, status, last_error)

    def _log(self, reference: str, status: str, last_error: str | None) -> None:
        log_event(
            "gl sync account",
            correlation_id=self.correlation_id,
            external_reference=reference,
            status=status,
            error=last_error,
        )


def _row(conn: Connection, reference: str | None, for_update: bool = False) -> RowMapping | None:
    query = select(gl_sync).where(gl_sync.c.erp_reference == reference)
    if for_update:
        query = query.with_for_update()
    return conn.execute(query).mappings().one_or_none()


def _set(conn: Connection, reference: str, **values: Any) -> None:
    values["updated_at"] = datetime.now(UTC)
    conn.execute(update(gl_sync).where(gl_sync.c.erp_reference == reference).values(**values))


def _status(row: RowMapping) -> AccountStatus:
    return AccountStatus(
        erp_reference=row["erp_reference"],
        code=row["erp_code"],
        name=row["name"],
        status=row["status"],
        attempts=row["attempts"],
        last_error=row["last_error"],
    )


def _rejection_reason(exc: FineractRejected, account: ErpAccount, parent: str | None) -> str:
    for code in exc.error_codes:
        template = _CONFLICT_REASONS.get(code)
        if template:
            return template.format(
                code=account.code, reference=account.external_reference, parent=parent
            )
    # Only codes: Fineract's own text can quote ids or data.
    codes = ", ".join(exc.error_codes) or "no reason given"
    return f"Fineract refused ({exc.status}): {codes}"


def _what_happened(exc: FineractError) -> str:
    """A short fixed description. The exception text can quote Fineract's body."""
    if isinstance(exc, FineractUnavailable):
        return "unreachable"
    if isinstance(exc, FineractServerError):
        return f"HTTP {exc.status}" if exc.status >= 500 else "unreadable answer"
    return "timeout"
