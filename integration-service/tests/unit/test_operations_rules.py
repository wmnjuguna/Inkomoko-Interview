"""The pure rules of the idempotency engine: keys, hashes, and what a repeated request gets.
The database side is in tests/integration/test_operations_db.py."""

from __future__ import annotations

import pytest

from integration_service.errors import ErrorCode
from integration_service.services.operations import (
    OP_TYPES,
    TAKEN_RULES,
    TYPE_NAMES,
    Conflict,
    InProgress,
    Replay,
    Retry,
    decide,
    format_loan_reference,
    make_op_key,
    operation_hash,
)
from integration_service.state_machine import STATES

HASH = "h1"


def test_op_key_is_type_colon_reference() -> None:
    assert make_op_key("DSB", "FIN-1") == "DSB:FIN-1"


def test_op_key_refuses_unknown_types() -> None:
    with pytest.raises(ValueError, match="XYZ"):
        make_op_key("XYZ", "FIN-1")


def test_every_type_has_a_human_name() -> None:
    assert TYPE_NAMES == {
        "CLI": "CLIENT_CREATION",
        "APP": "LOAN_APPLICATION",
        "APR": "APPROVAL",
        "DSB": "DISBURSEMENT",
        "RPY": "REPAYMENT",
    }
    assert set(OP_TYPES) == set(TYPE_NAMES)


def test_hash_ignores_key_order() -> None:
    assert operation_hash("p", {"a": 1, "b": 2}) == operation_hash("p", {"b": 2, "a": 1})


def test_hash_changes_with_the_data() -> None:
    assert operation_hash("p", {"amount": 100}) != operation_hash("p", {"amount": 101})


def test_hash_changes_with_the_partner() -> None:
    """Another partner reusing a reference gets a conflict, never someone else's answer."""
    assert operation_hash("a", {"amount": 1}) != operation_hash("b", {"amount": 1})


def test_loan_reference_format() -> None:
    assert format_loan_reference(2026, 1) == "LN-2026-000001"
    assert format_loan_reference(2027, 123456) == "LN-2027-123456"
    assert format_loan_reference(2026, 1234567) == "LN-2026-1234567"


@pytest.mark.parametrize("status", STATES)
def test_different_data_is_always_a_conflict(status: str) -> None:
    assert decide(status, "other", HASH, 200, {"x": 1}) == Conflict()


@pytest.mark.parametrize(
    "status", ["FINERACT_DONE", "ERP_PENDING", "COMPLETED", "NEEDS_RECONCILIATION"]
)
def test_finished_with_a_saved_answer_is_replayed(status: str) -> None:
    body = {"loan_reference": "LN-2026-000001", "erp_status": "PENDING"}
    assert decide(status, HASH, HASH, 201, body) == Replay(201, body)


def test_needs_reconciliation_without_a_saved_answer_is_in_progress() -> None:
    """Stuck before Fineract answered (too many attempts): nothing to replay yet."""
    assert decide("NEEDS_RECONCILIATION", HASH, HASH, None, None) == InProgress()


@pytest.mark.parametrize("status", ["RECEIVED", "FINERACT_PENDING"])
def test_running_is_in_progress(status: str) -> None:
    assert decide(status, HASH, HASH, None, None) == InProgress()


def test_failed_with_the_same_data_is_retried() -> None:
    assert decide("FAILED", HASH, HASH, None, None) == Retry()


def test_taken_rules_name_the_partner_error() -> None:
    assert TAKEN_RULES == {
        "operations_one_live_client": ErrorCode.CLIENT_UID_TAKEN,
        "operations_one_live_approval": ErrorCode.LOAN_ALREADY_APPROVED,
        "operations_one_live_disbursement": ErrorCode.LOAN_ALREADY_DISBURSED,
    }
