"""Pure rules for loans: request validation, Fineract bodies built from the product, reading
Fineract's loan into our names (never an id), money and dates, Fineract refusals, and the
answers for a repeated request."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from integration_service.clients.errors import FineractRejected
from integration_service.errors import ApiError, ErrorCode
from integration_service.schemas.loans import (
    ApprovalRequest,
    LoanApplicationRequest,
    LoanPath,
)
from integration_service.services.loans import (
    Answer,
    ProductTerms,
    answer_for_begin,
    application_rejection,
    approval_rejection,
    build_application_body,
    build_approval_body,
    fineract_date,
    kigali_today,
    loan_status_code,
    loan_view,
    parse_product,
    whole_rwf,
)
from integration_service.services.operations import (
    Conflict,
    InProgress,
    Operation,
    Replay,
    Taken,
)

ID_KEY = re.compile(r"(^id$|_id$|Id$)")


def id_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if ID_KEY.search(key) and key != "correlation_id":
                found.append(key)
            found += id_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            found += id_keys(inner)
    return found


PRODUCT = {
    "id": 1,
    "externalId": "PRD-SME-ASSET-FINANCE",
    "minPrincipal": 100000.0,
    "maxPrincipal": 50000000.0,
    "numberOfRepayments": 12,
    "repaymentEvery": 1,
    "repaymentFrequencyType": {"id": 2, "code": "repaymentFrequency.periodFrequencyType.months"},
    "interestRatePerPeriod": 18.0,
    "amortizationType": {"id": 1},
    "interestType": {"id": 0},
    "interestCalculationPeriodType": {"id": 1},
    "transactionProcessingStrategyCode": "mifos-standard-strategy",
}

ACTIVE_LOAN = {
    "id": 7,
    "accountNo": "000000007",
    "externalId": "LN-2026-000001",
    "clientId": 41,
    "clientExternalId": "SME-000001",
    "loanProductId": 1,
    "status": {
        "id": 300,
        "code": "loanStatusType.active",
        "value": "Active",
        "closedObligationsMet": False,
        "overpaid": False,
    },
    "principal": 1200000.0,
    "approvedPrincipal": 1200000.0,
    "currency": {"code": "RWF", "decimalPlaces": 0},
    "timeline": {
        "submittedOnDate": [2026, 10, 13],
        "approvedOnDate": [2026, 10, 13],
        "expectedDisbursementDate": [2026, 10, 13],
        "actualDisbursementDate": [2026, 10, 13],
        "expectedMaturityDate": [2027, 10, 13],
    },
    "summary": {
        "principalDisbursed": 1200000.0,
        "principalPaid": 82000.0,
        "principalOutstanding": 1118000.0,
        "interestCharged": 120195.0,
        "interestPaid": 18000.0,
        "interestOutstanding": 102195.0,
        "feeChargesOutstanding": 0,
        "penaltyChargesOutstanding": 0,
        "totalExpectedRepayment": 1320195.0,
        "totalRepayment": 100000.0,
        "totalOutstanding": 1220195.0,
        "totalOverdue": 0,
        "currency": {"code": "RWF"},
    },
}


# --- request validation ------------------------------------------------------------------


def application(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "external_reference": "APP-1",
        "client_uid": "SME-000001",
        "principal": 1200000,
    }
    body.update(changes)
    return body


def test_application_needs_whole_positive_rwf() -> None:
    request = LoanApplicationRequest.model_validate(application())
    assert request.principal == 1200000
    assert request.expected_disbursement_date is None


@pytest.mark.parametrize("principal", [0, -5, 1000.6, 1000.0, "1000", True, None])
def test_application_refuses_non_whole_or_non_positive_amounts(principal: Any) -> None:
    with pytest.raises(ValidationError):
        LoanApplicationRequest.model_validate(application(principal=principal))


@pytest.mark.parametrize(
    "changes",
    [
        {"external_reference": "bad ref"},
        {"client_uid": "nope"},
        {"interest_rate": 20},
        {"expected_disbursement_date": "13 October 2026"},
    ],
)
def test_application_refuses_bad_fields(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LoanApplicationRequest.model_validate(application(**changes))


def test_application_accepts_an_iso_date() -> None:
    request = LoanApplicationRequest.model_validate(
        application(expected_disbursement_date="2026-10-20")
    )
    assert request.expected_disbursement_date == date(2026, 10, 20)


def test_approval_request() -> None:
    assert ApprovalRequest.model_validate({"external_reference": "APR-1"}).approved_on is None
    request = ApprovalRequest.model_validate(
        {"external_reference": "APR-1", "approved_on": "2026-10-13"}
    )
    assert request.approved_on == date(2026, 10, 13)
    with pytest.raises(ValidationError):
        ApprovalRequest.model_validate({})
    with pytest.raises(ValidationError):
        ApprovalRequest.model_validate({"external_reference": "APR-1", "amount": 5})


@pytest.mark.parametrize("ref", ["LN-2026-000001", "LN-2027-123456"])
def test_loan_path_accepts_our_references(ref: str) -> None:
    assert LoanPath(loan_reference=ref).loan_reference == ref


@pytest.mark.parametrize("ref", ["7", "LN-26-000001", "LN-2026-1", "ln-2026-000001"])
def test_loan_path_refuses_other_values(ref: str) -> None:
    with pytest.raises(ValidationError):
        LoanPath(loan_reference=ref)


# --- product and Fineract bodies -----------------------------------------------------------


def test_product_terms_come_from_fineract() -> None:
    terms = parse_product(PRODUCT)
    assert terms == ProductTerms(
        product_id=1,
        min_principal=100000,
        max_principal=50000000,
        number_of_repayments=12,
        repayment_every=1,
        repayment_frequency_type=2,
        interest_rate_per_period=Decimal("18.0"),
        amortization_type=1,
        interest_type=0,
        interest_calculation_period_type=1,
        transaction_processing_strategy_code="mifos-standard-strategy",
    )


def test_application_body_uses_the_product_terms_and_our_loan_reference() -> None:
    body = build_application_body(
        parse_product(PRODUCT),
        fineract_client_id=41,
        principal=1200000,
        loan_reference="LN-2026-000001",
        submitted_on=date(2026, 10, 13),
        expected_disbursement_on=date(2026, 10, 20),
    )
    assert body == {
        "clientId": 41,
        "productId": 1,
        "principal": 1200000,
        "loanTermFrequency": 12,
        "loanTermFrequencyType": 2,
        "numberOfRepayments": 12,
        "repaymentEvery": 1,
        "repaymentFrequencyType": 2,
        "interestRatePerPeriod": 18.0,
        "amortizationType": 1,
        "interestType": 0,
        "interestCalculationPeriodType": 1,
        "transactionProcessingStrategyCode": "mifos-standard-strategy",
        "expectedDisbursementDate": "2026-10-20",
        "submittedOnDate": "2026-10-13",
        "loanType": "individual",
        "externalId": "LN-2026-000001",
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }


def test_approval_body() -> None:
    assert build_approval_body(date(2026, 10, 13), 1200000) == {
        "approvedOnDate": "2026-10-13",
        "approvedLoanAmount": 1200000,
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }


# --- money, dates, status -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(82000.0, 82000), (0, 0), ("1320195", 1320195), (None, None), (1000.6, 1001)],
)
def test_whole_rwf(value: Any, expected: int | None) -> None:
    assert whole_rwf(value) == expected


def test_fineract_dates() -> None:
    assert fineract_date([2026, 10, 13]) == date(2026, 10, 13)
    assert fineract_date(None) is None
    assert fineract_date("2026-10-13") is None


def test_kigali_today_is_two_hours_ahead_of_utc() -> None:
    assert kigali_today(datetime(2026, 10, 13, 21, 59, tzinfo=UTC)) == date(2026, 10, 13)
    assert kigali_today(datetime(2026, 10, 13, 22, 0, tzinfo=UTC)) == date(2026, 10, 14)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("loanStatusType.submitted.and.pending.approval", "SUBMITTED_AND_PENDING_APPROVAL"),
        ("loanStatusType.approved", "APPROVED"),
        ("loanStatusType.active", "ACTIVE"),
        ("loanStatusType.closed.obligations.met", "CLOSED_OBLIGATIONS_MET"),
        ("loanStatusType.overpaid", "OVERPAID"),
        (None, "UNKNOWN"),
    ],
)
def test_loan_status_codes(code: str | None, expected: str) -> None:
    assert loan_status_code(code) == expected


# --- loan view ---------------------------------------------------------------------------------


def test_loan_view_maps_fineract_balances_to_our_names() -> None:
    body = loan_view(ACTIVE_LOAN, "LN-2026-000001", "SME-000001", payoff_amount=1220195)
    assert body == {
        "loan_reference": "LN-2026-000001",
        "client_uid": "SME-000001",
        "status": {"code": "ACTIVE", "value": "Active"},
        "closed_obligations_met": False,
        "overpaid": False,
        "currency": "RWF",
        "principal": 1200000,
        "approved_principal": 1200000,
        "submitted_on": "2026-10-13",
        "approved_on": "2026-10-13",
        "expected_disbursement_on": "2026-10-13",
        "disbursed_on": "2026-10-13",
        "closed_on": None,
        "expected_maturity_on": "2027-10-13",
        "balances": {
            "principal_disbursed": 1200000,
            "principal_repaid": 82000,
            "principal_outstanding": 1118000,
            "interest_charged": 120195,
            "interest_repaid": 18000,
            "interest_outstanding": 102195,
            "fees_outstanding": 0,
            "penalties_outstanding": 0,
            "total_expected_repayment": 1320195,
            "total_repaid": 100000,
            "total_outstanding": 1220195,
            "total_overdue": 0,
            "overpaid_amount": 0,
        },
        "payoff_amount": 1220195,
    }
    assert id_keys(body) == []


def test_loan_view_before_disbursement_has_no_balances() -> None:
    loan = {k: v for k, v in ACTIVE_LOAN.items() if k != "summary"}
    loan["status"] = {"code": "loanStatusType.approved", "value": "Approved"}
    body = loan_view(loan, "LN-2026-000001", "SME-000001", payoff_amount=None)
    assert body["balances"] is None
    assert body["status"] == {"code": "APPROVED", "value": "Approved"}
    assert body["payoff_amount"] is None


def test_loan_view_shows_overpaid_amount() -> None:
    loan = {
        **ACTIVE_LOAN,
        "status": {"code": "loanStatusType.overpaid", "value": "Overpaid", "overpaid": True},
        "totalOverpaid": 5000.0,
    }
    body = loan_view(loan, "LN-2026-000001", "SME-000001", payoff_amount=None)
    assert body["overpaid"] is True
    assert body["balances"]["overpaid_amount"] == 5000


# --- Fineract refusals -------------------------------------------------------------------------


def rejected(status: int, *codes: str) -> FineractRejected:
    message = "Loan with id 7 and transaction 99"
    return FineractRejected(
        status,
        {
            "developerMessage": message,
            "errors": [
                {"userMessageGlobalisationCode": c, "developerMessage": message} for c in codes
            ],
        },
    )


def test_approving_twice_is_loan_already_approved() -> None:
    error = approval_rejection(
        rejected(400, "error.msg.loan.approve.account.is.not.submitted.and.pending.state"),
        "LN-2026-000001",
    )
    assert error.code == ErrorCode.LOAN_ALREADY_APPROVED
    assert error.status == 409
    assert "LN-2026-000001" in error.message


def test_application_refusals_never_quote_fineract() -> None:
    error = application_rejection(rejected(403, "error.msg.something.new"), "SME-000001")
    assert error.code == ErrorCode.VALIDATION_ERROR
    assert error.status == 422
    assert "7" not in error.message
    assert error.details == {"fineract_codes": ["error.msg.something.new"]}


def test_inactive_client_refusal_is_explained() -> None:
    error = application_rejection(
        rejected(403, "error.msg.client.not.active.exception"), "SME-000001"
    )
    assert error.status == 422
    assert "SME-000001" in error.message
    assert "not active" in error.message


# --- answers for a repeated request ----------------------------------------------------------


def operation(status: str) -> Operation:
    now = datetime(2026, 10, 13, tzinfo=UTC)
    return Operation(
        id=1,
        op_type="APP",
        external_reference="APP-1",
        op_key="APP:APP-1",
        partner="partner-a",
        status=status,
        client_uid="SME-000001",
        loan_reference="LN-2026-000001",
        attempts=1,
        recovery_lookups=0,
        request_payload=None,
        next_attempt_at=None,
        last_error=None,
        response_status=None,
        response_body=None,
        fineract_resource_id=7,
        fineract_transaction_id=99,
        erp_status=None,
        erp_transaction_id=None,
        correlation_id="c-1",
        created_at=now,
        updated_at=now,
    )


def test_replay_is_200_with_the_saved_body() -> None:
    # A create answers 201 the first time; every repeat gets 200 and the same body.
    assert answer_for_begin(Replay(201, {"a": 1}), "APP-1") == Answer(200, {"a": 1})


def test_in_progress_is_202_with_the_status_and_no_ids() -> None:
    answer = answer_for_begin(InProgress(operation("FINERACT_PENDING")), "APP-1")
    assert answer.status == 202
    assert answer.body["external_reference"] == "APP-1"
    assert answer.body["status"] == "FINERACT_PENDING"
    assert answer.body["type"] == "LOAN_APPLICATION"
    assert answer.body["loan_reference"] == "LN-2026-000001"
    assert id_keys(answer.body) == []
    assert "7" not in str(answer.body.values()).replace("LN-2026-000001", "")


def test_waiting_for_a_manual_check_is_not_called_still_running() -> None:
    """NEEDS_RECONCILIATION with no saved answer: nothing is running and nothing will run
    until someone repairs it by hand. Saying 202 "still running" would make a partner poll
    forever."""
    with pytest.raises(ApiError) as caught:
        answer_for_begin(InProgress(operation("NEEDS_RECONCILIATION")), "APP-1")
    assert caught.value.status == 409
    assert caught.value.code == ErrorCode.NEEDS_RECONCILIATION
    assert "still running" not in caught.value.message
    assert "manual" in caught.value.message
    assert caught.value.details == {"status": "NEEDS_RECONCILIATION", "type": "LOAN_APPLICATION"}


def test_in_progress_without_an_operation() -> None:
    answer = answer_for_begin(InProgress(None), "APP-1")
    assert answer.status == 202
    assert answer.body["status"] is None


def test_conflict_is_409_idempotency_conflict() -> None:
    with pytest.raises(ApiError) as caught:
        answer_for_begin(Conflict(), "APP-1")
    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert caught.value.status == 409
    assert "APP-1" in caught.value.message


@pytest.mark.parametrize(
    "code",
    [ErrorCode.CLIENT_UID_TAKEN, ErrorCode.LOAN_ALREADY_APPROVED, ErrorCode.LOAN_ALREADY_DISBURSED],
)
def test_taken_is_409_with_its_code(code: ErrorCode) -> None:
    with pytest.raises(ApiError) as caught:
        answer_for_begin(Taken(code, "COMPLETED"), "APP-1")
    assert caught.value.code == code
    assert caught.value.status == 409
    assert caught.value.details == {"holder_status": "COMPLETED"}
