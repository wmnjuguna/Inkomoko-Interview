"""execute_fineract_write: one Fineract write, and what we know about it afterwards.

The rule under test: never call Fineract a second time for one operation without first
looking it up, when the earlier outcome is unknown. Fakes stand in for the call and the
lookup, so every branch can be chosen."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from integration_service.clients.errors import (
    FineractConflict,
    FineractError,
    FineractNotFound,
    FineractRejected,
    FineractServerError,
    FineractTimeout,
    FineractUnavailable,
    InvalidIdempotencyKey,
)
from integration_service.services.fineract_calls import (
    Done,
    NotSent,
    Rejected,
    Unknown,
    execute_fineract_write,
    idempotency_key,
)

FOUND = {"transaction": "found"}
WRITTEN = {"transaction": "written"}


class FakeCall:
    def __init__(self, outcome: dict[str, str] | FineractError) -> None:
        self.outcome = outcome
        self.keys: list[str] = []

    def __call__(self, key: str) -> dict[str, str]:
        self.keys.append(key)
        if isinstance(self.outcome, FineractError):
            raise self.outcome
        return self.outcome


class FakeLookup:
    def __init__(self, outcome: dict[str, str] | FineractError | None) -> None:
        self.outcome = outcome
        self.calls = 0

    def __call__(self) -> dict[str, str] | None:
        self.calls += 1
        if isinstance(self.outcome, FineractError):
            raise self.outcome
        return self.outcome


def run(
    call: Callable[[str], dict[str, str]],
    lookup: Callable[[], dict[str, str] | None],
    attempt: int = 1,
    check_first: bool = False,
) -> object:
    return execute_fineract_write(call, lookup, "DSB:FIN-1", attempt, check_first=check_first)


def test_key_is_op_key_and_attempt() -> None:
    assert idempotency_key("DSB:FIN-1", 3) == "DSB:FIN-1:3"


def test_key_longer_than_fineract_allows_is_refused_before_sending() -> None:
    call = FakeCall(WRITTEN)
    with pytest.raises(InvalidIdempotencyKey):
        execute_fineract_write(call, FakeLookup(None), "DSB:" + "R" * 40, 10**6)
    assert call.keys == []


def test_success_is_done_and_sends_the_attempt_key_once() -> None:
    call, lookup = FakeCall(WRITTEN), FakeLookup(None)
    outcome = run(call, lookup, attempt=2)
    assert outcome == Done(WRITTEN, found_by_lookup=False)
    assert call.keys == ["DSB:FIN-1:2"]
    assert lookup.calls == 0


def test_unreachable_is_not_sent_without_a_lookup() -> None:
    call, lookup = FakeCall(FineractUnavailable("connect refused")), FakeLookup(FOUND)
    outcome = run(call, lookup)
    assert isinstance(outcome, NotSent)
    assert outcome.reason == "Fineract not reachable; nothing was sent"
    assert lookup.calls == 0
    assert len(call.keys) == 1


@pytest.mark.parametrize(
    "error",
    [
        FineractTimeout("read timeout"),
        FineractServerError(500, {}),
        FineractServerError(200, {}, "unreadable"),
        FineractRejected(425, {}),
    ],
    ids=["timeout", "5xx", "unreadable", "425-still-running"],
)
def test_unknown_outcome_found_by_lookup_is_done(error: FineractError) -> None:
    call, lookup = FakeCall(error), FakeLookup(FOUND)
    assert run(call, lookup) == Done(FOUND, found_by_lookup=True)
    assert len(call.keys) == 1
    assert lookup.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        FineractTimeout("read timeout"),
        FineractServerError(503, {}),
        FineractServerError(200, {}, "unreadable"),
        FineractRejected(425, {}),
    ],
    ids=["timeout", "5xx", "unreadable", "425-still-running"],
)
def test_unknown_outcome_not_found_right_away_stays_unknown(error: FineractError) -> None:
    """Fineract may still be working on it (425 says so outright; a timeout does not stop
    its transaction). An empty lookup at this moment proves nothing: never NotSent."""
    call, lookup = FakeCall(error), FakeLookup(None)
    outcome = run(call, lookup)
    assert isinstance(outcome, Unknown)
    assert "not found yet" in outcome.reason
    assert len(call.keys) == 1


@pytest.mark.parametrize(
    "lookup_error",
    [FineractTimeout("t"), FineractUnavailable("u"), FineractServerError(500, {})],
    ids=["timeout", "unreachable", "5xx"],
)
def test_unknown_outcome_with_a_failed_lookup_stays_unknown(lookup_error: FineractError) -> None:
    call, lookup = FakeCall(FineractTimeout("read timeout")), FakeLookup(lookup_error)
    outcome = run(call, lookup)
    assert isinstance(outcome, Unknown)
    assert "lookup failed" in outcome.reason
    assert len(call.keys) == 1


def test_clear_refusal_is_rejected_without_a_lookup() -> None:
    error = FineractRejected(400, {"errors": [{"userMessageGlobalisationCode": "x.y"}]})
    call, lookup = FakeCall(error), FakeLookup(FOUND)
    outcome = run(call, lookup)
    assert outcome == Rejected(error)
    assert lookup.calls == 0


def test_not_found_is_rejected() -> None:
    error = FineractNotFound(404, {})
    assert run(FakeCall(error), FakeLookup(None)) == Rejected(error)


def test_conflict_found_by_lookup_is_done() -> None:
    """A 409 can mean an earlier attempt already wrote it (same externalId)."""
    call, lookup = FakeCall(FineractConflict(409, {})), FakeLookup(FOUND)
    assert run(call, lookup) == Done(FOUND, found_by_lookup=True)


def test_conflict_not_found_by_lookup_is_not_sent() -> None:
    """Fineract's 409 is a lost concurrency race: it rolled back and wrote nothing. That is
    worth sending again, not a refusal of the partner's data."""
    outcome = run(FakeCall(FineractConflict(409, {})), FakeLookup(None))
    assert isinstance(outcome, NotSent)
    assert "conflict" in outcome.reason


def test_conflict_with_a_failed_lookup_stays_unknown() -> None:
    outcome = run(FakeCall(FineractConflict(409, {})), FakeLookup(FineractTimeout("t")))
    assert isinstance(outcome, Unknown)


def test_check_first_finds_it_and_never_calls() -> None:
    call, lookup = FakeCall(WRITTEN), FakeLookup(FOUND)
    assert run(call, lookup, attempt=2, check_first=True) == Done(FOUND, found_by_lookup=True)
    assert call.keys == []


def test_check_first_with_a_failed_lookup_never_calls() -> None:
    call, lookup = FakeCall(WRITTEN), FakeLookup(FineractServerError(502, {}))
    outcome = run(call, lookup, attempt=2, check_first=True)
    assert isinstance(outcome, Unknown)
    assert call.keys == []


def test_check_first_not_found_calls_once_and_never_looks_again() -> None:
    call, lookup = FakeCall(FineractTimeout("t")), FakeLookup(None)
    outcome = run(call, lookup, attempt=2, check_first=True)
    # Looked up before (nothing), sent once, timed out, looked up after (nothing yet).
    assert isinstance(outcome, Unknown)
    assert call.keys == ["DSB:FIN-1:2"]
    assert lookup.calls == 2


def test_reasons_never_quote_fineract_text() -> None:
    secret = "loan 12345 client 678"
    for error in (
        FineractTimeout(secret),
        FineractServerError(500, {"developerMessage": secret}, secret),
        FineractUnavailable(secret),
    ):
        outcome = run(FakeCall(error), FakeLookup(FineractTimeout(secret)))
        assert isinstance(outcome, NotSent | Unknown)
        assert "12345" not in outcome.reason
