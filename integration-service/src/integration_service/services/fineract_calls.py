"""One Fineract write for one operation, and what we know about it afterwards.

The rule: never call Fineract a second time for an operation without first looking it up
by its externalId, when the earlier outcome is unknown.

execute_fineract_write() calls the write at most once and answers with one of:
- Done: Fineract has it. Either it said yes, or a lookup found it after an unclear answer.
- NotSent: we know nothing happened. Fineract was not reachable (nothing left our side), or
  it answered 409 (a lost concurrency race: it rolled back) and a lookup found nothing.
  The caller marks the operation FAILED; the same reference may retry.
- Rejected: Fineract clearly refused (4xx). Nothing was written. The caller marks it FAILED
  and turns the error into a partner error.
- Unknown: the answer was unclear (timeout, 5xx, unreadable, 425), and the lookup right
  after it failed or found nothing yet. Fineract can still be working on the request: a
  client timeout does not stop its transaction, and 425 says outright that it is still
  running. So an empty lookup at that moment proves nothing. The operation stays
  FINERACT_PENDING; recovery looks it up again once the request is stale, and only that
  later lookup may decide FAILED. It is never sent again blindly.

Special cases:
- 425 means Fineract is still running a request with this Idempotency-Key: the outcome is
  unknown, so it is looked up like a timeout.
- 409 can mean an earlier attempt already wrote the same externalId, so it is looked up too.
- check_first=True looks it up before sending (for an attempt after an unclear outcome, and
  for recovery). Found means no call at all.

Reasons are our own words. They never quote Fineract's text, which can hold Fineract ids."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from integration_service.clients.errors import (
    FineractConflict,
    FineractError,
    FineractRejected,
    FineractServerError,
    FineractUnavailable,
)
from integration_service.clients.fineract import check_idempotency_key

STILL_RUNNING = 425


@dataclass(frozen=True)
class Done[T]:
    result: T
    found_by_lookup: bool


@dataclass(frozen=True)
class NotSent:
    reason: str


@dataclass(frozen=True)
class Rejected:
    error: FineractRejected


@dataclass(frozen=True)
class Unknown:
    reason: str


type Outcome[T] = Done[T] | NotSent | Rejected | Unknown


def idempotency_key(op_key: str, attempt: int) -> str:
    """<op key>:<attempt>, for example DSB:FIN-1:2."""
    return f"{op_key}:{attempt}"


def execute_fineract_write[T](
    call: Callable[[str], T],
    lookup: Callable[[], T | None],
    op_key: str,
    attempt: int,
    *,
    check_first: bool = False,
) -> Outcome[T]:
    """call(idempotency_key) does the write; lookup() finds it by externalId (None = not
    there). InvalidIdempotencyKey is raised before anything is sent."""
    key = idempotency_key(op_key, attempt)
    check_idempotency_key(key)

    if check_first:
        found = _look_up(lookup)
        if not isinstance(found, _NotThere):
            return found

    try:
        result = call(key)
    except FineractUnavailable:
        return NotSent("Fineract not reachable; nothing was sent")
    except FineractRejected as exc:
        if exc.status == STILL_RUNNING:
            return _after_unclear(lookup, "Fineract was still running an earlier attempt")
        if isinstance(exc, FineractConflict):
            after = _look_up(lookup)
            if isinstance(after, _NotThere):
                return NotSent("Fineract answered a concurrency conflict; nothing was written")
            return after
        return Rejected(exc)
    except FineractError as exc:
        return _after_unclear(lookup, f"Fineract did not answer clearly ({_what(exc)})")
    return Done(result, found_by_lookup=False)


@dataclass(frozen=True)
class _NotThere:
    pass


def _look_up[T](lookup: Callable[[], T | None]) -> Done[T] | Unknown | _NotThere:
    try:
        found = lookup()
    except FineractError as exc:
        return Unknown(f"lookup failed ({_what(exc)}); will check again before any retry")
    if found is None:
        return _NotThere()
    return Done(found, found_by_lookup=True)


def _after_unclear[T](lookup: Callable[[], T | None], what: str) -> Outcome[T]:
    found = _look_up(lookup)
    if isinstance(found, _NotThere):
        return Unknown(f"{what}; not found yet, Fineract may still be working on it")
    if isinstance(found, Unknown):
        return Unknown(f"{what}; {found.reason}")
    return found


def _what(exc: FineractError) -> str:
    """A short fixed description. The exception text can quote Fineract's body."""
    if isinstance(exc, FineractUnavailable):
        return "unreachable"
    if isinstance(exc, FineractServerError):
        return f"HTTP {exc.status}" if exc.status >= 500 else "unreadable answer"
    if isinstance(exc, FineractRejected):
        return f"HTTP {exc.status}"
    return "timeout"
