"""Pure rules for loans: request validation, the checks and terms taken from Fineract's
templates (fixtures in tests/fixtures/fineract_templates, changed here so a range or an amount
can only come from the template), reading Fineract's loan into our names (never an id), money
and dates, Fineract refusals, and the answers for a repeated request."""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from integration_service.clients.errors import (
    FineractRejected,
    FineractServerError,
    FineractTimeout,
)
from integration_service.errors import ApiError, ErrorCode
from integration_service.schemas.loans import (
    ApprovalRequest,
    LoanApplicationRequest,
    LoanPath,
)
from integration_service.services import loans as loans_service
from integration_service.services import templates
from integration_service.services.loans import (
    Answer,
    ApprovalTerms,
    answer_for_begin,
    application_error,
    application_rejection,
    approval_rejection,
    approval_terms,
    build_approval_body,
    fineract_date,
    kigali_today,
    loan_status_code,
    loan_view,
    past_expected_date_error,
    payoff_amount,
    read_approval_form,
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


FIXTURES = Path(__file__).parent.parent / "fixtures" / "fineract_templates"
TODAY = date(2026, 10, 13)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def application_template(**product: Any) -> dict[str, Any]:
    """The real loan template, with the product part changed."""
    data = copy.deepcopy(fixture("loan_template_individual_product.json"))
    data["product"].update(product)
    return data


class FakeFineract:
    """Answers GETs from a dict of path -> body (or exception). Records each call."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.calls.append((path, params))
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


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


# --- rules from Fineract's templates -------------------------------------------------------


def test_the_shared_constants_are_the_template_ones_and_no_product_copy_is_left() -> None:
    assert loans_service.FINERACT_DATE_FORMAT is templates.FINERACT_DATE_FORMAT
    assert loans_service.FINERACT_LOCALE is templates.FINERACT_LOCALE
    assert loans_service.build_application_body is templates.build_application_body
    for gone in ("parse_product", "ProductTerms", "PRODUCT_EXTERNAL_ID"):
        assert not hasattr(loans_service, gone), gone


def test_the_principal_range_comes_from_the_template() -> None:
    form = templates.parse_loan_application_form(
        application_template(minPrincipal=300000.0, maxPrincipal=900000.0)
    )
    assert application_error(form, 300000, TODAY, TODAY) is None
    assert application_error(form, 900000, TODAY, TODAY) is None

    error = application_error(form, 1200000, TODAY, TODAY)
    assert error is not None
    assert (error.code, error.status) == (ErrorCode.VALIDATION_ERROR, 422)
    assert error.details is not None
    problems = error.details["problems"]
    assert problems[0]["field"] == "principal"
    assert "300000 and 900000" in problems[0]["problem"]
    assert error.message.startswith("principal: ")

    below = application_error(form, 299999, TODAY, TODAY)
    assert below is not None and below.details is not None
    assert below.details["problems"][0]["field"] == "principal"


def test_a_template_with_one_limit_only_checks_that_limit() -> None:
    form = templates.parse_loan_application_form(
        application_template(minPrincipal=2000000.0, maxPrincipal=None)
    )
    assert application_error(form, 90000000, TODAY, TODAY) is None
    error = application_error(form, 1999999, TODAY, TODAY)
    assert error is not None and error.details is not None
    assert error.details["problems"] == [
        {"field": "principal", "problem": "must be at least 2000000 RWF (the product range)"}
    ]


def test_a_past_expected_date_is_a_field_problem() -> None:
    error = past_expected_date_error()
    assert (error.code, error.status) == (ErrorCode.VALIDATION_ERROR, 422)
    assert error.details == {
        "problems": [
            {
                "field": "expected_disbursement_date",
                "problem": "cannot be in the past (Kigali date)",
            }
        ]
    }


def test_the_approval_takes_the_template_amount_and_default_date() -> None:
    data = fixture("loan_approval_template.json")
    data["approvalAmount"] = 1750000.0
    data["approvalDate"] = [2026, 10, 12]
    form = templates.parse_approval_form(data)

    assert approval_terms(form, None, TODAY) == ApprovalTerms(date(2026, 10, 12), 1750000)
    assert approval_terms(form, date(2026, 10, 13), TODAY) == ApprovalTerms(TODAY, 1750000)


def test_the_approval_date_without_a_template_default_is_today() -> None:
    data = fixture("loan_approval_template.json")
    data["approvalDate"] = None
    form = templates.parse_approval_form(data)
    assert approval_terms(form, None, TODAY) == ApprovalTerms(TODAY, 2500000)


def test_a_future_approval_date_is_a_field_problem() -> None:
    form = templates.parse_approval_form(fixture("loan_approval_template.json"))
    with pytest.raises(ApiError) as caught:
        approval_terms(form, date(2026, 10, 14), TODAY)
    assert caught.value.status == 422
    assert caught.value.details == {
        "problems": [{"field": "approved_on", "problem": "cannot be in the future (Kigali date)"}]
    }


def test_the_approval_form_is_read_by_external_id() -> None:
    path = "/loans/external-id/LN-2026-000001/template"
    fake = FakeFineract({path: fixture("loan_approval_template.json")})
    form = read_approval_form(fake, "LN-2026-000001", None)
    assert form.approved_amount == 2500000
    assert fake.calls == [(path, {"templateType": "approval"})]


def test_an_approval_template_without_an_amount_is_an_unexpected_answer() -> None:
    data = fixture("loan_approval_template.json")
    del data["approvalAmount"]
    fake = FakeFineract({"/loans/external-id/LN-2026-000001/template": data})
    with pytest.raises(FineractServerError):
        read_approval_form(fake, "LN-2026-000001", None)


PAYOFF_PATH = "/loans/external-id/LN-2026-000123/transactions/template"


def test_payoff_amount_is_the_prepay_template_amount() -> None:
    fake = FakeFineract({PAYOFF_PATH: fixture("loan_prepay_template.json")})
    assert payoff_amount(fake, "LN-2026-000123", None) == 1270195
    assert fake.calls == [(PAYOFF_PATH, {"command": "prepayLoan"})]


@pytest.mark.parametrize(
    "answer",
    [
        FineractTimeout("no answer"),
        {**fixture("loan_prepay_template.json"), "amount": 1270195.5},
        {k: v for k, v in fixture("loan_prepay_template.json").items() if k != "currency"},
    ],
)
def test_payoff_amount_is_best_effort(answer: Any) -> None:
    """A failed read or a template we cannot read leaves payoff_amount out (null)."""
    assert payoff_amount(FakeFineract({PAYOFF_PATH: answer}), "LN-2026-000123", None) is None


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
