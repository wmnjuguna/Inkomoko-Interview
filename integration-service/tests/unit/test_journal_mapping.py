"""Part H: the journal entry is built only from Fineract's own transaction data.

The transaction bodies below have the shape Fineract 1.15.0 returned in the probe
(GET /loans/external-id/{loan}/transactions/external-id/{txn}): floats for amounts, some
zero portions as the integer 0, and dates as [year, month, day]."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from integration_service.mapping.journal import (
    FineractTransaction,
    GlRoleAccounts,
    GlRolesConfigError,
    NeedsReconciliation,
    build_entry,
    load_gl_role_accounts,
    plan_entry,
)

LOAN = "LN-2026-000001"
ACCOUNTS = GlRoleAccounts(
    bank="1100", loan_portfolio="1200", interest_income="4100", overpayment_liability="2100"
)
REPO_PRODUCT = Path(__file__).resolve().parents[3] / "fineract/configuration/loan-product.json"


def txn(**changes: Any) -> FineractTransaction:
    body: dict[str, Any] = {
        "id": 17,
        "loanId": 4,
        "officeId": 2,
        "type": {"id": 2, "code": "loanTransactionType.repayment", "repayment": True},
        "date": [2026, 10, 13],
        "currency": {"code": "RWF", "decimalPlaces": 0},
        "amount": 100000.0,
        "principalPortion": 82000.0,
        "interestPortion": 18000.0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
        "unrecognizedIncomePortion": 0,
        "outstandingLoanBalance": 1118000.0,
        "externalId": "RPY:PAY-1",
        "manuallyReversed": False,
        "reversalExternalId": None,
        "reversedOnDate": None,
    }
    body.update(changes)
    return FineractTransaction.model_validate(body)


def disbursement(**changes: Any) -> FineractTransaction:
    base: dict[str, Any] = {
        "type": {"id": 1, "code": "loanTransactionType.disbursement", "disbursement": True},
        "amount": 1200000.0,
        "principalPortion": 0,
        "interestPortion": 0,
        "netDisbursalAmount": 1200000.0,
        "externalId": "DSB:FIN-1",
    }
    base.update(changes)
    return txn(**base)


def lines_of(entry: Any) -> list[dict[str, Any]]:
    return entry.to_erp_body()["lines"]  # type: ignore[no-any-return]


def totals(entry: Any) -> tuple[int, int]:
    lines = lines_of(entry)
    return (
        sum(line.get("debit", 0) for line in lines),
        sum(line.get("credit", 0) for line in lines),
    )


# --- the transaction model --------------------------------------------------------------


def test_reads_fineract_floats_and_date_arrays() -> None:
    t = txn()
    assert t.transaction_date == date(2026, 10, 13)
    assert t.amount == 100000
    assert t.principal_portion == 82000
    assert t.fee_charges_portion == 0
    assert t.type_code == "loanTransactionType.repayment"
    assert t.external_id == "RPY:PAY-1"
    assert t.reversed is False


def test_reads_an_iso_date_too() -> None:
    assert txn(date="2026-10-13").transaction_date == date(2026, 10, 13)


def test_missing_portions_count_as_zero() -> None:
    body = {
        "type": {"code": "loanTransactionType.disbursement"},
        "date": [2026, 10, 13],
        "amount": 5000,
    }
    t = FineractTransaction.model_validate(body)
    assert t.principal_portion == 0
    assert t.overpayment_portion == 0


def test_reversal_shows_through_either_field() -> None:
    assert txn(manuallyReversed=True).reversed
    assert txn(reversalExternalId="RPY:PAY-1:REV").reversed
    assert txn(reversedOnDate=[2026, 10, 14]).reversed


# --- disbursement ------------------------------------------------------------------------


def test_disbursement_is_dr_loan_portfolio_cr_bank() -> None:
    entry = build_entry("DSB", "DSB:FIN-1", disbursement(), ACCOUNTS, LOAN)
    body = entry.to_erp_body()
    assert body == {
        "external_reference": "DSB:FIN-1",
        "entry_date": "2026-10-13",
        "source": "DISBURSEMENT",
        "description": f"Disbursement {LOAN}",
        "lines": [
            {"account_code": "1200", "debit": 1200000},
            {"account_code": "1100", "credit": 1200000},
        ],
    }


def test_disbursement_with_a_principal_portion_equal_to_the_amount_is_fine() -> None:
    entry = build_entry(
        "DSB", "DSB:FIN-1", disbursement(principalPortion=1200000.0), ACCOUNTS, LOAN
    )
    assert totals(entry) == (1200000, 1200000)


def test_disbursement_with_interest_is_not_guessed() -> None:
    with pytest.raises(NeedsReconciliation, match="disbursement"):
        build_entry("DSB", "DSB:FIN-1", disbursement(interestPortion=10.0), ACCOUNTS, LOAN)


# --- repayment ---------------------------------------------------------------------------


def test_repayment_splits_principal_and_interest() -> None:
    entry = build_entry("RPY", "RPY:PAY-1", txn(), ACCOUNTS, LOAN)
    body = entry.to_erp_body()
    assert body["source"] == "REPAYMENT"
    assert body["description"] == f"Repayment {LOAN}"
    assert body["entry_date"] == "2026-10-13"
    assert body["external_reference"] == "RPY:PAY-1"
    assert body["lines"] == [
        {"account_code": "1100", "debit": 100000},
        {"account_code": "1200", "credit": 82000},
        {"account_code": "4100", "credit": 18000},
    ]


def test_interest_only_repayment_has_no_principal_line() -> None:
    entry = build_entry(
        "RPY",
        "RPY:PAY-1",
        txn(amount=18000.0, principalPortion=0, interestPortion=18000.0),
        ACCOUNTS,
        LOAN,
    )
    assert lines_of(entry) == [
        {"account_code": "1100", "debit": 18000},
        {"account_code": "4100", "credit": 18000},
    ]


def test_principal_only_repayment_has_no_interest_line() -> None:
    entry = build_entry(
        "RPY",
        "RPY:PAY-1",
        txn(amount=50000.0, principalPortion=50000.0, interestPortion=0),
        ACCOUNTS,
        LOAN,
    )
    assert lines_of(entry) == [
        {"account_code": "1100", "debit": 50000},
        {"account_code": "1200", "credit": 50000},
    ]


def test_overpayment_goes_to_the_liability_account() -> None:
    entry = build_entry(
        "RPY",
        "RPY:PAY-1",
        txn(amount=5000.0, principalPortion=0, interestPortion=0, overpaymentPortion=5000.0),
        ACCOUNTS,
        LOAN,
    )
    assert lines_of(entry) == [
        {"account_code": "1100", "debit": 5000},
        {"account_code": "2100", "credit": 5000},
    ]


def test_payoff_with_overpayment_has_three_credit_lines() -> None:
    entry = build_entry(
        "RPY",
        "RPY:PAY-1",
        txn(
            amount=924195.0,
            principalPortion=862636.0,
            interestPortion=56559.0,
            overpaymentPortion=5000.0,
        ),
        ACCOUNTS,
        LOAN,
    )
    assert lines_of(entry) == [
        {"account_code": "1100", "debit": 924195},
        {"account_code": "1200", "credit": 862636},
        {"account_code": "4100", "credit": 56559},
        {"account_code": "2100", "credit": 5000},
    ]


@pytest.mark.parametrize(
    ("amount", "principal", "interest", "over"),
    [
        (100000, 82000, 18000, 0),
        (250000, 204364, 45636, 0),
        (50000, 50000, 0, 0),
        (919195, 862636, 56559, 0),
        (5000, 0, 0, 5000),
        (1, 1, 0, 0),
    ],
)
def test_every_measured_split_balances(
    amount: int, principal: int, interest: int, over: int
) -> None:
    entry = build_entry(
        "RPY",
        "RPY:PAY-1",
        txn(
            amount=float(amount),
            principalPortion=float(principal),
            interestPortion=float(interest),
            overpaymentPortion=float(over),
        ),
        ACCOUNTS,
        LOAN,
    )
    assert totals(entry) == (amount, amount)
    assert all((line.get("debit") or line.get("credit") or 0) > 0 for line in lines_of(entry))


def test_account_codes_come_from_the_accounts_given() -> None:
    other = GlRoleAccounts(
        bank="1111", loan_portfolio="1222", interest_income="4111", overpayment_liability="2111"
    )
    entry = build_entry("RPY", "RPY:PAY-1", txn(overpaymentPortion=0), other, LOAN)
    assert [line["account_code"] for line in lines_of(entry)] == ["1111", "1222", "4111"]


# --- what is never guessed -----------------------------------------------------------------


@pytest.mark.parametrize("field", ["feeChargesPortion", "penaltyChargesPortion"])
def test_fees_or_penalties_need_reconciliation(field: str) -> None:
    t = txn(amount=101000.0, **{field: 1000.0})
    with pytest.raises(NeedsReconciliation, match=r"fee|penalt"):
        build_entry("RPY", "RPY:PAY-1", t, ACCOUNTS, LOAN)


def test_a_reversed_transaction_needs_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="reversed"):
        build_entry("RPY", "RPY:PAY-1", txn(manuallyReversed=True), ACCOUNTS, LOAN)


def test_portions_that_do_not_add_up_need_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="add up"):
        build_entry("RPY", "RPY:PAY-1", txn(amount=100001.0), ACCOUNTS, LOAN)


def test_unrecognized_income_is_not_guessed() -> None:
    with pytest.raises(NeedsReconciliation, match="unrecognized"):
        build_entry("RPY", "RPY:PAY-1", txn(unrecognizedIncomePortion=5.0), ACCOUNTS, LOAN)


def test_a_repayment_typed_as_disbursement_needs_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="type"):
        build_entry("RPY", "RPY:PAY-1", disbursement(), ACCOUNTS, LOAN)


def test_a_disbursement_typed_as_repayment_needs_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="type"):
        build_entry("DSB", "DSB:FIN-1", txn(), ACCOUNTS, LOAN)


def test_an_accrual_needs_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="type"):
        build_entry(
            "RPY", "RPY:PAY-1", txn(type={"code": "loanTransactionType.accrual"}), ACCOUNTS, LOAN
        )


def test_an_unknown_operation_type_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="CLI"):
        build_entry("CLI", "CLI:X", txn(), ACCOUNTS, LOAN)


def test_a_fraction_of_a_franc_needs_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="whole"):
        build_entry(
            "RPY",
            "RPY:PAY-1",
            txn(amount=100000.5, principalPortion=82000.5),
            ACCOUNTS,
            LOAN,
        )


def test_another_currency_needs_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="RWF"):
        build_entry(
            "RPY", "RPY:PAY-1", txn(currency={"code": "USD", "decimalPlaces": 2}), ACCOUNTS, LOAN
        )


def test_a_zero_amount_needs_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="above zero"):
        build_entry(
            "RPY",
            "RPY:PAY-1",
            txn(amount=0, principalPortion=0, interestPortion=0),
            ACCOUNTS,
            LOAN,
        )


def test_a_transaction_with_another_external_id_needs_reconciliation() -> None:
    with pytest.raises(NeedsReconciliation, match="externalId"):
        build_entry("RPY", "RPY:OTHER", txn(), ACCOUNTS, LOAN)


def test_reasons_never_show_fineract_ids() -> None:
    with pytest.raises(NeedsReconciliation) as caught:
        build_entry("RPY", "RPY:PAY-1", txn(amount=100001.0, id=987654), ACCOUNTS, LOAN)
    assert "987654" not in caught.value.reason


# --- plan_entry ------------------------------------------------------------------------------


def test_plan_entry_carries_the_entry_and_erp_status_pending() -> None:
    plan = plan_entry("RPY", "RPY:PAY-1", txn(), ACCOUNTS, LOAN)
    assert plan.erp_status == "PENDING"
    assert plan.problem is None
    assert (
        plan.outbox_entry() == build_entry("RPY", "RPY:PAY-1", txn(), ACCOUNTS, LOAN).to_erp_body()
    )


def test_plan_entry_turns_a_problem_into_needs_reconciliation() -> None:
    plan = plan_entry(
        "RPY", "RPY:PAY-1", txn(feeChargesPortion=10.0, amount=100010.0), ACCOUNTS, LOAN
    )
    assert plan.erp_status == "NEEDS_RECONCILIATION"
    assert plan.entry is None
    assert plan.problem is not None and "fee" in plan.problem
    stored = plan.outbox_entry()
    assert stored["unbuilt"] is True
    assert stored["reason"] == plan.problem
    assert stored["external_reference"] == "RPY:PAY-1"


# --- the GL role accounts loader ------------------------------------------------------------


def test_loads_the_role_accounts_from_the_repo_product_file() -> None:
    assert load_gl_role_accounts(REPO_PRODUCT) == ACCOUNTS


def write_product(tmp_path: Path, gl_accounts: dict[str, Any]) -> Path:
    path = tmp_path / "loan-product.json"
    path.write_text(json.dumps({"name": "x", "glAccounts": gl_accounts}))
    return path


def full_roles(**changes: str) -> dict[str, str]:
    roles = {
        "fundSourceAccountId": "GL-1100",
        "loanPortfolioAccountId": "GL-1200",
        "interestOnLoanAccountId": "GL-4100",
        "overpaymentLiabilityAccountId": "GL-2100",
    }
    roles.update(changes)
    return roles


def test_codes_follow_the_erp_references(tmp_path: Path) -> None:
    path = write_product(tmp_path, full_roles(fundSourceAccountId="GL-1150"))
    assert load_gl_role_accounts(path).bank == "1150"


def test_a_missing_role_is_an_error(tmp_path: Path) -> None:
    roles = full_roles()
    del roles["overpaymentLiabilityAccountId"]
    with pytest.raises(GlRolesConfigError, match="overpaymentLiabilityAccountId"):
        load_gl_role_accounts(write_product(tmp_path, roles))


def test_a_reference_that_is_not_gl_code_is_an_error(tmp_path: Path) -> None:
    path = write_product(tmp_path, full_roles(interestOnLoanAccountId="4100"))
    with pytest.raises(GlRolesConfigError, match="interestOnLoanAccountId"):
        load_gl_role_accounts(path)


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(GlRolesConfigError, match="not found"):
        load_gl_role_accounts(tmp_path / "nope.json")


def test_unreadable_json_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "loan-product.json"
    path.write_text("{not json")
    with pytest.raises(GlRolesConfigError):
        load_gl_role_accounts(path)
