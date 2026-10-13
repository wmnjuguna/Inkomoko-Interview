"""One Fineract write per loan at a time (layer L3 in ARCHITECTURE.md).

loan_lock() holds a Postgres session-level advisory lock on its own connection for as long
as the block runs. A second writer to the same loan waits, up to timeout_seconds, then gets
LoanLockTimeoutError. The lock works across threads, gunicorn workers and service copies, since
they all share the one database.

Hold it only around the Fineract call and the lookups that go with it. Status changes use
their own short transactions, so they never wait on this lock.

The lock is always released: on the way out, or, if the unlock fails, by closing the
connection (Postgres drops a session's locks when the session ends)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError

DEFAULT_TIMEOUT_SECONDS = 30.0


class LoanLockTimeoutError(RuntimeError):
    """Another write to this loan held the lock for longer than the timeout."""


def loan_lock_key(loan_reference: str) -> int:
    """A stable signed 64-bit key from 'loan:<reference>'. Python's hash() changes per run,
    so it cannot be used across workers."""
    digest = hashlib.sha256(f"loan:{loan_reference}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def loan_lock(
    engine: Engine, loan_reference: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> Iterator[None]:
    key = loan_lock_key(loan_reference)
    conn = engine.connect()
    try:
        try:
            with conn.begin():
                # lock_timeout covers advisory lock waits; local to this short transaction.
                conn.execute(
                    text("SELECT set_config('lock_timeout', :ms, true)"),
                    {"ms": f"{max(1, int(timeout_seconds * 1000))}ms"},
                )
                conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        except OperationalError as exc:
            if getattr(exc.orig, "sqlstate", None) == "55P03":  # lock_not_available
                raise LoanLockTimeoutError(
                    f"Another write to loan {loan_reference} is still running"
                ) from exc
            raise
        try:
            yield
        finally:
            _unlock(conn, key)
    finally:
        conn.close()


def _unlock(conn: Connection, key: int) -> None:
    try:
        with conn.begin():
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    except Exception:
        # The session may still hold the lock: end it rather than return it to the pool.
        conn.invalidate()
        raise
