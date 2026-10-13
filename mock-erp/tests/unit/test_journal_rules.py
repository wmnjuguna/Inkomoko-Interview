"""Journal rules that need no database: line sides, sizes, reversal links, hashing, balance."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mock_erp.errors import ErpError, ErrorCode
from mock_erp.schemas.journal import JournalEntryIn, JournalLineIn
from mock_erp.services.journal import check_balanced, entry_hash


def entry_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "external_reference": "FIN-2026-000002",
        "entry_date": "2026-10-12",
        "source": "REPAYMENT",
        "description": "Repayment LN-2026-000001",
        "reverses": None,
        "lines": [
            {"account_code": "1100", "debit": 458400},
            {"account_code": "1200", "credit": 383400},
            {"account_code": "4100", "credit": 75000},
        ],
    }
    body.update(changes)
    return body


# Line sides


def test_a_line_with_only_a_debit_is_valid() -> None:
    line = JournalLineIn.model_validate({"account_code": "1100", "debit": 10})
    assert (line.debit, line.credit) == (10, 0)


@pytest.mark.parametrize(
    "line",
    [
        {"account_code": "1100", "debit": 10, "credit": 10},
        {"account_code": "1100"},
        {"account_code": "1100", "debit": 0, "credit": 0},
        {"account_code": "1100", "debit": -5},
        {"account_code": "1100", "credit": -5},
        {"account_code": "1100", "debit": 10**15 + 1},
        {"account_code": "1100", "debit": 10.5},
    ],
)
def test_a_line_needs_exactly_one_positive_whole_side(line: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        JournalLineIn.model_validate(line)


# Entry shape


def test_the_sample_entry_is_valid() -> None:
    entry = JournalEntryIn.model_validate(entry_body())
    assert entry.source == "REPAYMENT"
    assert len(entry.lines) == 3


@pytest.mark.parametrize("count", [1, 51])
def test_an_entry_needs_two_to_fifty_lines(count: int) -> None:
    lines = [{"account_code": "1100", "debit": 1}] * count
    with pytest.raises(ValidationError):
        JournalEntryIn.model_validate(entry_body(lines=lines))


@pytest.mark.parametrize("reference", ["", "-starts-with-dash", "has space", "x" * 65])
def test_external_reference_format_is_checked(reference: str) -> None:
    with pytest.raises(ValidationError):
        JournalEntryIn.model_validate(entry_body(external_reference=reference))


def test_description_is_at_most_500_chars() -> None:
    with pytest.raises(ValidationError):
        JournalEntryIn.model_validate(entry_body(description="x" * 501))


def test_unknown_source_is_refused() -> None:
    with pytest.raises(ValidationError):
        JournalEntryIn.model_validate(entry_body(source="GIFT"))


# Reversal links


def test_a_reversal_must_name_the_entry_it_reverses() -> None:
    with pytest.raises(ValidationError, match="reverses"):
        JournalEntryIn.model_validate(entry_body(source="REVERSAL", reverses=None))


def test_only_a_reversal_may_set_reverses() -> None:
    with pytest.raises(ValidationError, match="reverses"):
        JournalEntryIn.model_validate(entry_body(source="MANUAL", reverses="FIN-2026-000001"))


def test_a_reversal_with_reverses_is_valid() -> None:
    entry = JournalEntryIn.model_validate(entry_body(source="REVERSAL", reverses="FIN-1"))
    assert entry.reverses == "FIN-1"


# Request hash


def test_key_order_does_not_change_the_entry_hash() -> None:
    body = entry_body()
    reordered = dict(reversed(list(body.items())))
    reordered["lines"] = [dict(reversed(list(line.items()))) for line in body["lines"]]
    assert entry_hash(JournalEntryIn.model_validate(body)) == entry_hash(
        JournalEntryIn.model_validate(reordered)
    )


def test_line_order_does_not_change_the_entry_hash() -> None:
    body = entry_body()
    shuffled = entry_body(lines=list(reversed(body["lines"])))
    assert entry_hash(JournalEntryIn.model_validate(body)) == entry_hash(
        JournalEntryIn.model_validate(shuffled)
    )


def test_explicit_zero_side_gives_the_same_hash() -> None:
    plain = entry_body()
    explicit = entry_body(
        lines=[
            {"account_code": "1100", "debit": 458400, "credit": 0},
            {"account_code": "1200", "debit": 0, "credit": 383400},
            {"account_code": "4100", "debit": 0, "credit": 75000},
        ]
    )
    assert entry_hash(JournalEntryIn.model_validate(plain)) == entry_hash(
        JournalEntryIn.model_validate(explicit)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"entry_date": "2026-10-13"},
        {"description": "Other"},
        {"source": "MANUAL"},
        {
            "lines": [
                {"account_code": "1100", "debit": 458401},
                {"account_code": "1200", "credit": 383401},
                {"account_code": "4100", "credit": 75000},
            ]
        },
    ],
)
def test_any_business_change_changes_the_entry_hash(change: dict[str, Any]) -> None:
    assert entry_hash(JournalEntryIn.model_validate(entry_body())) != entry_hash(
        JournalEntryIn.model_validate(entry_body(**change))
    )


# Balance


def test_a_balanced_entry_passes() -> None:
    check_balanced(JournalEntryIn.model_validate(entry_body()))


def test_an_unbalanced_entry_is_refused_with_both_totals() -> None:
    body = entry_body(
        lines=[{"account_code": "1100", "debit": 100}, {"account_code": "1200", "credit": 90}]
    )
    with pytest.raises(ErpError) as caught:
        check_balanced(JournalEntryIn.model_validate(body))
    assert caught.value.code == ErrorCode.ENTRY_NOT_BALANCED
    assert caught.value.status == 422
    assert caught.value.details == {"debits": 100, "credits": 90}
    assert caught.value.message == "Debits are 100 and credits are 90."
