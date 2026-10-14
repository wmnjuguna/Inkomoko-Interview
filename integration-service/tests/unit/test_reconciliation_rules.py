"""Part K: the pure reconciliation rules. Fineract transactions and ERP entries are fixtures in
the shapes the live systems send (dates as [y, m, d] from Fineract, ISO strings from the ERP)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from integration_service.errors import ApiError
from integration_service.mapping.journal import GlRoleAccounts
from integration_service.schemas.reconciliation import ReconciliationRequest
from integration_service.services.reconciliation import (
    DateRange,
    LoanTransaction,
    OurState,
    Outcome,
    References,
    erp_lookups,
    reconcile,
    reference_candidates,
    scope_of,
)

ACCOUNTS = GlRoleAccounts(
    bank="1100", loan_portfolio="1200", interest_income="4100", overpayment_liability="2100"
)
DAY = date(2026, 10, 14)
TODAY = DateRange(DAY, DAY)
LOAN = "LN-2026-000123"


def txn(
    kind: str,
    amount: int,
    external_id: str | None,
    principal: int = 0,
    interest: int = 0,
    on: date = DAY,
    **extra: Any,
) -> LoanTransaction:
    data: dict[str, Any] = {
        "id": 77,
        "type": {"code": f"loanTransactionType.{kind}"},
        "date": [on.year, on.month, on.day],
        "currency": {"code": "RWF"},
        "amount": float(amount),
        "principalPortion": principal,
        "interestPortion": interest,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
        "manuallyReversed": False,
        **extra,
    }
    if external_id is not None:
        data["externalId"] = external_id
    return LoanTransaction(LOAN, data)


def entry(
    reference: str,
    lines: list[tuple[str, int, int]],
    source: str = "REPAYMENT",
    on: date = DAY,
    reverses: str | None = None,
) -> dict[str, Any]:
    return {
        "erp_transaction_id": "3476f214-afac-4e31-907a-e519cfe567a8",
        "external_reference": reference,
        "entry_date": on.isoformat(),
        "source": source,
        "reverses": reverses,
        "total": sum(d for _, d, _ in lines),
        "lines": [
            {"account_code": a, "account_name": "x", "debit": d, "credit": c} for a, d, c in lines
        ],
    }


DSB_LINES = [("1200", 1_000_000, 0), ("1100", 0, 1_000_000)]
RPY_LINES = [("1100", 100_000, 0), ("1200", 0, 80_000), ("4100", 0, 20_000)]


def disbursement(key: str | None = "DSB:FIN-1", **extra: Any) -> LoanTransaction:
    return txn("disbursement", 1_000_000, key, **extra)


def repayment(key: str | None = "RPY:FIN-2", **extra: Any) -> LoanTransaction:
    return txn("repayment", 100_000, key, principal=80_000, interest=20_000, **extra)


def only(report: Any) -> Any:
    assert len(report.items) == 1, report.items
    return report.items[0]


# --- the four outcomes ------------------------------------------------------------------------


def test_matched_disbursement_and_repayment() -> None:
    report = reconcile(
        [disbursement(), repayment()],
        [entry("DSB:FIN-1", DSB_LINES, "DISBURSEMENT"), entry("RPY:FIN-2", RPY_LINES)],
        {},
        ACCOUNTS,
        TODAY,
    )
    assert [(i.key, i.outcome) for i in report.items] == [
        ("DSB:FIN-1", Outcome.MATCHED),
        ("RPY:FIN-2", Outcome.MATCHED),
    ]
    assert report.summary()["MATCHED"] == 2
    assert all(i.reasons == () for i in report.items)


def test_missing_in_erp_for_a_foreign_external_id() -> None:
    item = only(reconcile([repayment("MANUAL-9")], [], {}, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.MISSING_IN_ERP
    assert item.loan_reference == LOAN
    assert any("not one of our operation keys" in r for r in item.reasons)
    # The lines the ERP should have, from Fineract's own split.
    assert [(x.account_code, x.debit, x.credit) for x in item.expected_lines] == RPY_LINES


def test_missing_in_erp_without_external_id() -> None:
    item = only(reconcile([repayment(None)], [], {}, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.MISSING_IN_ERP
    assert item.key is None
    assert any("no externalId" in r for r in item.reasons)


def test_missing_in_fineract() -> None:
    item = only(reconcile([], [entry("RPY:GHOST-1", RPY_LINES)], {}, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.MISSING_IN_FINERACT
    assert item.fineract is None and item.erp is not None
    assert item.erp.total == 100_000


def test_amount_mismatch_names_each_difference() -> None:
    wrong = [("1100", 100_000, 0), ("1200", 0, 90_000), ("4100", 0, 10_000)]
    item = only(reconcile([repayment()], [entry("RPY:FIN-2", wrong)], {}, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.AMOUNT_MISMATCH
    assert "account 1200 credit: ERP 90000, from Fineract 80000" in item.reasons
    assert "account 4100 credit: ERP 10000, from Fineract 20000" in item.reasons


def test_total_difference_is_a_mismatch() -> None:
    wrong = [("1200", 999_000, 0), ("1100", 0, 999_000)]
    item = only(
        reconcile(
            [disbursement()], [entry("DSB:FIN-1", wrong, "DISBURSEMENT")], {}, ACCOUNTS, TODAY
        )
    )
    assert item.outcome is Outcome.AMOUNT_MISMATCH
    assert "total: ERP 999000, from Fineract 1000000" in item.reasons


def test_wrong_source_or_date_is_a_mismatch() -> None:
    late = date(2026, 10, 15)
    report = reconcile(
        [disbursement()],
        [entry("DSB:FIN-1", DSB_LINES, "MANUAL", on=late)],
        {},
        ACCOUNTS,
        DateRange(DAY, late),
    )
    item = only(report)
    assert item.outcome is Outcome.AMOUNT_MISMATCH
    assert "source: ERP MANUAL, from Fineract DISBURSEMENT" in item.reasons
    assert "entry_date: ERP 2026-10-15, from Fineract 2026-10-14" in item.reasons


def test_entry_that_cannot_be_built_is_not_called_matched() -> None:
    fee = repayment(feeChargesPortion=5_000)
    item = only(reconcile([fee], [entry("RPY:FIN-2", RPY_LINES)], {}, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.AMOUNT_MISMATCH
    assert any("fee or penalty" in r for r in item.reasons)
    assert item.expected_lines is None


# --- what is compared -------------------------------------------------------------------------


def test_accruals_and_other_types_are_not_compared() -> None:
    accrual = txn("accrual", 500, None, interest=500)
    waive = txn("waiveInterest", 300, None)
    report = reconcile([accrual, waive, repayment()], [], {}, ACCOUNTS, TODAY)
    assert [i.key for i in report.items] == ["RPY:FIN-2"]
    assert report.not_compared_fineract == {"ACCRUAL": 1, "WAIVE_INTEREST": 1}


def test_erp_entries_that_are_not_operation_keys_are_not_compared() -> None:
    report = reconcile([], [entry("FIN-2026-000002", RPY_LINES, "MANUAL")], {}, ACCOUNTS, TODAY)
    assert report.items == []
    assert report.not_compared_erp == 1


def test_no_fineract_numeric_id_in_an_item() -> None:
    item = only(reconcile([repayment()], [entry("RPY:FIN-2", RPY_LINES)], {}, ACCOUNTS, TODAY))
    assert "77" not in repr(item)


# --- scope ------------------------------------------------------------------------------------


def test_date_range_keeps_items_with_either_side_in_range() -> None:
    old = date(2026, 9, 1)
    report = reconcile(
        [repayment(on=old), disbursement(on=old)],
        [entry("RPY:FIN-2", RPY_LINES, on=DAY)],  # the ERP side is in range
        {},
        ACCOUNTS,
        TODAY,
    )
    item = only(report)
    assert item.key == "RPY:FIN-2"
    assert item.outcome is Outcome.AMOUNT_MISMATCH  # the dates differ


def test_references_select_op_keys_partner_references_and_reversals() -> None:
    scope = References(frozenset({"FIN-2", "DSB:FIN-1", "MANUAL-9"}))
    assert scope.selects("RPY:FIN-2")
    assert scope.selects("DSB:FIN-2")
    assert scope.selects("DSB:FIN-1:REV")
    assert scope.selects("MANUAL-9")
    assert not scope.selects("RPY:FIN-3")
    assert not scope.selects(None)
    assert reference_candidates(["FIN-2"]) == [
        "FIN-2",
        "FIN-2:REV",
        "DSB:FIN-2",
        "DSB:FIN-2:REV",
        "RPY:FIN-2",
        "RPY:FIN-2:REV",
    ]


def test_erp_lookups_ask_for_in_scope_keys_the_erp_list_did_not_hold() -> None:
    old = date(2026, 9, 1)
    keys = erp_lookups(
        [disbursement(), repayment(), repayment("RPY:OLD", on=old), repayment(None)],
        {"DSB:FIN-1"},
        TODAY,
    )
    assert keys == ["RPY:FIN-2"]


# --- our own state ----------------------------------------------------------------------------


def test_waiting_in_our_outbox_is_in_flight_not_missing() -> None:
    ours = {"RPY:FIN-2": OurState("ERP_PENDING", "PENDING")}
    item = only(reconcile([repayment()], [], ours, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.IN_FLIGHT
    assert item.ours == ours["RPY:FIN-2"]
    assert reconcile([repayment()], [], ours, ACCOUNTS, TODAY).summary()["IN_FLIGHT"] == 1


def test_outbox_given_up_is_missing_with_the_reason() -> None:
    ours = {"RPY:FIN-2": OurState("NEEDS_RECONCILIATION", "NEEDS_RECONCILIATION")}
    item = only(reconcile([repayment()], [], ours, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.MISSING_IN_ERP
    assert any("NEEDS_RECONCILIATION" in r for r in item.reasons)


def test_sent_by_us_but_absent_in_erp_says_so() -> None:
    ours = {"RPY:FIN-2": OurState("COMPLETED", "SENT")}
    item = only(reconcile([repayment()], [], ours, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.MISSING_IN_ERP
    assert any("SENT" in r for r in item.reasons)


# --- reversals --------------------------------------------------------------------------------

REVERSED = {"manuallyReversed": True, "reversedOnDate": [2026, 10, 14]}
RPY_REV_LINES = [("1100", 0, 100_000), ("1200", 80_000, 0), ("4100", 20_000, 0)]


def test_reversed_with_entry_and_opposite_entry_both_match() -> None:
    report = reconcile(
        [repayment(**REVERSED)],
        [
            entry("RPY:FIN-2", RPY_LINES),
            entry("RPY:FIN-2:REV", RPY_REV_LINES, "REVERSAL", reverses="RPY:FIN-2"),
        ],
        {},
        ACCOUNTS,
        TODAY,
    )
    assert [(i.key, i.outcome, i.kind) for i in report.items] == [
        ("RPY:FIN-2", Outcome.MATCHED, "REPAYMENT"),
        ("RPY:FIN-2:REV", Outcome.MATCHED, "REVERSAL"),
    ]


def test_reversed_in_fineract_but_only_the_original_in_erp() -> None:
    report = reconcile(
        [repayment(**REVERSED)], [entry("RPY:FIN-2", RPY_LINES)], {}, ACCOUNTS, TODAY
    )
    outcomes = {i.key: i.outcome for i in report.items}
    assert outcomes == {"RPY:FIN-2": Outcome.MATCHED, "RPY:FIN-2:REV": Outcome.MISSING_IN_ERP}


def test_reversal_entry_with_same_side_lines_is_a_mismatch() -> None:
    report = reconcile(
        [repayment(**REVERSED)],
        [
            entry("RPY:FIN-2", RPY_LINES),
            entry("RPY:FIN-2:REV", RPY_LINES, "REVERSAL", reverses="RPY:FIN-2"),
        ],
        {},
        ACCOUNTS,
        TODAY,
    )
    rev = next(i for i in report.items if i.key == "RPY:FIN-2:REV")
    assert rev.outcome is Outcome.AMOUNT_MISMATCH
    assert "account 1100 debit: ERP 100000, from Fineract 0" in rev.reasons


def test_reversed_in_fineract_and_absent_in_erp_nets_to_zero() -> None:
    item = only(reconcile([repayment(**REVERSED)], [], {}, ACCOUNTS, TODAY))
    assert item.outcome is Outcome.MATCHED
    assert any("net" in r for r in item.reasons)


def test_erp_reversal_of_a_live_fineract_transaction_is_missing_in_fineract() -> None:
    report = reconcile(
        [repayment()],
        [
            entry("RPY:FIN-2", RPY_LINES),
            entry("RPY:FIN-2:REV", RPY_REV_LINES, "REVERSAL", reverses="RPY:FIN-2"),
        ],
        {},
        ACCOUNTS,
        TODAY,
    )
    rev = next(i for i in report.items if i.key == "RPY:FIN-2:REV")
    assert rev.outcome is Outcome.MISSING_IN_FINERACT
    assert any("not reversed" in r for r in rev.reasons)


# --- the request ------------------------------------------------------------------------------


def request(**body: Any) -> ReconciliationRequest:
    return ReconciliationRequest.model_validate(body)


def test_scope_from_a_date_range() -> None:
    assert scope_of(request(**{"from": "2026-10-01", "to": "2026-10-31"})) == DateRange(
        date(2026, 10, 1), date(2026, 10, 31)
    )


def test_scope_from_references() -> None:
    assert scope_of(request(references=["FIN-1", "DSB:FIN-2"])) == References(
        frozenset({"FIN-1", "DSB:FIN-2"})
    )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "body"),
        ({"from": "2026-10-01"}, "to"),
        ({"to": "2026-10-01"}, "from"),
        ({"from": "2026-10-02", "to": "2026-10-01"}, "to"),
        ({"from": "2026-10-01", "to": "2026-11-01"}, "to"),
        ({"from": "2026-10-01", "to": "2026-10-02", "references": ["X"]}, "references"),
    ],
)
def test_bad_scope_is_a_422_naming_the_field(body: dict[str, Any], field: str) -> None:
    with pytest.raises(ApiError) as caught:
        scope_of(request(**body))
    assert caught.value.status == 422
    assert caught.value.details["problems"][0]["field"] == field


def test_too_many_references_is_refused_by_the_model() -> None:
    with pytest.raises(ValueError, match="at most 100"):
        request(references=[f"R-{n}" for n in range(101)])
