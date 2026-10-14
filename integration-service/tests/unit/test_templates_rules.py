"""Fineract templates as typed forms (services/templates.py): parsing the real 1.15 answers
saved in tests/fixtures/fineract_templates, the checks of a partner value against a form,
the partner views (never a Fineract id), and the fetch calls against a fake Fineract."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from integration_service.clients.errors import FineractNotFound, FineractServerError
from integration_service.errors import ApiError, ErrorCode
from integration_service.services import templates as t

FIXTURES = Path(__file__).parent.parent / "fixtures" / "fineract_templates"
TODAY = date(2026, 10, 13)
ID_KEY = re.compile(r"(^id$|_id$|Id$)")


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def no_ids(value: Any) -> None:
    """No key that looks like an id, anywhere in a partner answer."""
    if isinstance(value, dict):
        for key, inner in value.items():
            assert not ID_KEY.search(key), f"id-like key {key} in a partner answer"
            no_ids(inner)
    elif isinstance(value, list):
        for inner in value:
            no_ids(inner)


def field(view: dict[str, Any], name: str) -> dict[str, Any]:
    found = [f for f in view["fields"] if f["name"] == name]
    assert len(found) == 1, f"no field {name}"
    return found[0]


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


# --- parsing ----------------------------------------------------------------------------------


def test_choice_partner_code_drops_fineract_prefixes() -> None:
    assert t.choice_code("repaymentFrequency.periodFrequencyType.months") == "MONTHS"
    assert t.choice_code("amortizationType.equal.installments") == "EQUAL_INSTALLMENTS"
    assert t.choice_code("interestType.declining.balance") == "DECLINING_BALANCE"
    assert t.choice_code("interestRateFrequency.periodFrequencyType.years") == "YEARS"
    assert t.choice_code("legalFormType.person") == "PERSON"


def test_client_form_from_the_real_template() -> None:
    form = t.parse_client_form(fixture("clients_template.json"))
    assert form.activation_date == TODAY
    assert [o.name for o in form.offices] == ["Kigali Main Office"]
    assert form.office_id("Kigali Main Office") == 2
    assert form.office_id("Nowhere") is None


def test_loan_application_form_takes_ranges_and_defaults_from_the_product() -> None:
    form = t.parse_loan_application_form(fixture("loan_template_individual_product.json"))
    assert form.product_id == 1
    assert form.product_external_id == "PRD-SME-ASSET-FINANCE"
    assert form.product_name == "SME Asset Finance"
    assert form.product_short_name == "SMEA"
    assert form.currency == t.Currency("RWF", "Rwanda Franc", 0, 1)
    assert form.principal == t.IntRange(default=5_000_000, minimum=100_000, maximum=50_000_000)
    assert form.number_of_repayments == t.IntRange(default=12, minimum=12, maximum=12)
    assert form.interest_rate_per_period == t.RateRange(
        default=Decimal(18), minimum=Decimal(18), maximum=Decimal(18)
    )
    assert form.repayment_every == 1
    assert form.repayment_frequency.code == "MONTHS"
    assert form.repayment_frequency.fineract_id == 2
    assert form.loan_term == 12
    assert form.loan_term_frequency.code == "MONTHS"
    assert form.interest_rate_frequency.code == "YEARS"
    assert form.amortization.code == "EQUAL_INSTALLMENTS"
    assert form.amortization.fineract_id == 1
    assert form.interest_type.code == "DECLINING_BALANCE"
    assert form.interest_type.fineract_id == 0
    assert form.interest_calculation_period.code == "SAME_AS_REPAYMENT_PERIOD"
    assert form.transaction_processing_strategy_code == "mifos-standard-strategy"
    assert form.expected_disbursement_date == TODAY
    assert form.client_uid == "PROBE-000001"


def test_loan_application_form_needs_the_product_part() -> None:
    with pytest.raises(FineractServerError):
        t.parse_loan_application_form(fixture("loan_template_individual_no_product.json"))


def test_approval_form_takes_date_amount_and_expected_date_from_the_template() -> None:
    form = t.parse_approval_form(fixture("loan_approval_template.json"))
    assert form.approved_on == TODAY
    assert form.approved_amount == 2_500_000
    assert form.expected_disbursement_date == date(2026, 10, 20)
    assert form.currency.code == "RWF"


def test_disbursement_form_takes_expected_date_and_approved_amount_from_the_template() -> None:
    form = t.parse_disbursement_form(fixture("loan_disburse_template.json"))
    assert form.expected_date == date(2026, 10, 20)
    assert form.amount == 2_000_000


def test_repayment_form_is_the_next_installment() -> None:
    form = t.parse_repayment_form(fixture("loan_repayment_template.json"))
    assert form.due_date == date(2026, 11, 13)
    assert form.due_amount == 60016
    assert form.principal_portion == 60016
    assert form.interest_portion == 0
    assert form.fee_portion == 0
    assert form.penalty_portion == 0
    assert form.payment_type_id("money transfer") == 1
    # System payment types are for Fineract's own adjustments, not for partners.
    assert form.payment_type_id("Repayment Adjustment Refund") is None
    assert [p.name for p in form.partner_payment_types] == ["Money Transfer"]


def test_payoff_form_takes_fineract_payoff_figure_and_split() -> None:
    form = t.parse_payoff_form(fixture("loan_prepay_template.json"))
    assert form.on_date == TODAY
    assert form.amount == 1_270_195
    assert form.principal_portion == 1_168_000
    assert form.interest_portion == 102_195
    assert form.outstanding_principal == 1_168_000
    closed = t.parse_payoff_form(fixture("loan_prepay_template_closed.json"))
    assert closed.amount == 0


@pytest.mark.parametrize(
    "parse",
    [
        t.parse_client_form,
        t.parse_loan_application_form,
        t.parse_approval_form,
        t.parse_disbursement_form,
        t.parse_repayment_form,
        t.parse_payoff_form,
    ],
)
def test_an_unreadable_template_is_an_unexpected_fineract_answer(parse: Any) -> None:
    with pytest.raises(FineractServerError):
        parse(["not", "a", "template"])


def test_amounts_with_cents_are_refused_for_a_whole_unit_currency() -> None:
    data = fixture("loan_approval_template.json")
    data["approvalAmount"] = 1000.5
    with pytest.raises(FineractServerError):
        t.parse_approval_form(data)


# --- checks -----------------------------------------------------------------------------------


def application_form() -> t.LoanApplicationForm:
    return t.parse_loan_application_form(fixture("loan_template_individual_product.json"))


def test_application_within_the_product_range_passes() -> None:
    assert t.check_application(application_form(), 5_000_000, TODAY, TODAY) == []
    assert t.check_application(application_form(), 100_000, None, TODAY) == []
    assert t.check_application(application_form(), 50_000_000, date(2026, 12, 1), TODAY) == []


def test_application_outside_the_product_range_names_the_field() -> None:
    low = t.check_application(application_form(), 99_999, TODAY, TODAY)
    assert low == [
        t.FieldProblem("principal", "must be between 100000 and 50000000 RWF (the product range)")
    ]
    high = t.check_application(application_form(), 50_000_001, TODAY, TODAY)
    assert [p.field for p in high] == ["principal"]


def test_application_date_in_the_past_is_refused() -> None:
    problems = t.check_application(application_form(), 5_000_000, date(2026, 10, 12), TODAY)
    assert [p.field for p in problems] == ["expected_disbursement_date"]


def test_amount_must_be_a_multiple_of_the_currency_step() -> None:
    form = application_form()
    stepped = t.LoanApplicationForm(
        **{**form.__dict__, "currency": t.Currency("RWF", "Rwanda Franc", 0, 100)}
    )
    problems = t.check_application(stepped, 5_000_050, TODAY, TODAY)
    assert problems == [t.FieldProblem("principal", "must be a multiple of 100 RWF")]


def test_approval_refuses_a_future_date_or_one_after_the_expected_disbursement() -> None:
    form = t.parse_approval_form(fixture("loan_approval_template.json"))
    assert t.check_approval(form, None, TODAY) == []
    assert t.check_approval(form, TODAY, TODAY) == []
    assert [p.field for p in t.check_approval(form, date(2026, 10, 14), TODAY)] == ["approved_on"]
    # The template's expected disbursement date is 2026-10-20: approving after it is refused.
    later = date(2026, 10, 21)
    assert [p.field for p in t.check_approval(form, later, later)] == ["approved_on"]


def test_disbursement_refuses_a_future_date() -> None:
    assert t.check_disbursement(None, TODAY) == []
    assert [p.field for p in t.check_disbursement(date(2026, 10, 14), TODAY)] == [
        "transaction_date"
    ]


def test_repayment_lists_every_problem_with_amount_date_and_payment_type() -> None:
    form = t.parse_repayment_form(fixture("loan_repayment_template.json"))
    assert t.check_repayment(form, 60016, None, None, TODAY) == []
    assert t.check_repayment(form, 1, TODAY, "Money Transfer", TODAY) == []
    problems = t.check_repayment(form, 0, date(2026, 10, 14), "Cash", TODAY)
    assert [p.field for p in problems] == ["amount", "transaction_date", "payment_type"]
    assert problems[2].problem == "must be one of: Money Transfer"


def test_problems_become_one_validation_error() -> None:
    error = t.validation_error(
        [t.FieldProblem("principal", "too low"), t.FieldProblem("expected_disbursement_date", "x")]
    )
    assert isinstance(error, ApiError)
    assert error.code == ErrorCode.VALIDATION_ERROR
    assert error.status == 422
    assert error.message == "principal: too low"
    assert error.details == {
        "problems": [
            {"field": "principal", "problem": "too low"},
            {"field": "expected_disbursement_date", "problem": "x"},
        ]
    }


def test_step_availability_follows_the_loan_status() -> None:
    assert t.step_available(t.Step.APPROVAL, "SUBMITTED_AND_PENDING_APPROVAL")
    assert not t.step_available(t.Step.APPROVAL, "APPROVED")
    assert t.step_available(t.Step.DISBURSEMENT, "APPROVED")
    assert t.step_available(t.Step.REPAYMENT, "ACTIVE")
    assert t.step_available(t.Step.PAYOFF, "ACTIVE")
    assert not t.step_available(t.Step.PAYOFF, "CLOSED_OBLIGATIONS_MET")


# --- the Fineract body ------------------------------------------------------------------------


def test_application_body_uses_the_template_terms() -> None:
    body = t.build_application_body(
        application_form(),
        fineract_client_id=7,
        principal=5_000_000,
        loan_reference="LN-2026-000001",
        submitted_on=TODAY,
        expected_disbursement_on=TODAY,
    )
    assert body == {
        "clientId": 7,
        "productId": 1,
        "principal": 5_000_000,
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
        "expectedDisbursementDate": "2026-10-13",
        "submittedOnDate": "2026-10-13",
        "loanType": "individual",
        "externalId": "LN-2026-000001",
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }


# --- partner views ----------------------------------------------------------------------------


def test_client_form_view() -> None:
    view = t.client_form_view(t.parse_client_form(fixture("clients_template.json")))
    no_ids(view)
    assert view["form"] == "client"
    assert view["submit_to"] == "POST /api/clients"
    assert view["office"] == "Kigali Main Office"
    assert view["activation_date"] == "2026-10-13"
    uid = field(view, "client_uid")
    assert uid["type"] == "string"
    assert uid["required"] is True
    assert uid["pattern"] == "^[A-Z]{2,10}-[0-9]{6}$"
    assert field(view, "first_name")["max_length"] == 50
    assert field(view, "external_reference")["max_length"] == 40


def test_loan_application_form_view() -> None:
    view = t.loan_application_form_view(application_form(), "PROBE-000001", TODAY)
    no_ids(view)
    assert view["form"] == "loan_application"
    assert view["client_uid"] == "PROBE-000001"
    assert view["submit_to"] == "POST /api/loans"
    assert view["product"] == {
        "name": "SME Asset Finance",
        "short_name": "SMEA",
        "description": application_form().product_description,
        "currency": {
            "code": "RWF",
            "name": "Rwanda Franc",
            "decimal_places": 0,
            "in_multiples_of": 1,
        },
    }
    principal = field(view, "principal")
    assert principal == {
        "name": "principal",
        "type": "integer",
        "required": True,
        "read_only": False,
        "default": 5_000_000,
        "minimum": 100_000,
        "maximum": 50_000_000,
        "multiple_of": 1,
        "pattern": None,
        "max_length": None,
        "options": None,
        "description": principal["description"],
    }
    assert field(view, "client_uid")["default"] == "PROBE-000001"
    expected = field(view, "expected_disbursement_date")
    assert expected["type"] == "date"
    assert expected["required"] is False
    assert expected["default"] == "2026-10-13"
    assert expected["minimum"] == "2026-10-13"
    repayments = field(view, "number_of_repayments")
    assert repayments["read_only"] is True
    assert (repayments["default"], repayments["minimum"], repayments["maximum"]) == (12, 12, 12)
    rate = field(view, "interest_rate_per_period")
    assert (rate["type"], rate["default"], rate["minimum"], rate["maximum"]) == (
        "number",
        18.0,
        18.0,
        18.0,
    )
    frequency = field(view, "repayment_frequency")
    assert frequency["default"] == "MONTHS"
    assert frequency["options"] == [{"value": "MONTHS", "label": "Months"}]
    assert field(view, "amortization")["default"] == "EQUAL_INSTALLMENTS"
    assert field(view, "interest_type")["default"] == "DECLINING_BALANCE"


def test_loan_application_form_view_without_a_client() -> None:
    view = t.loan_application_form_view(application_form(), None, TODAY)
    assert view["client_uid"] is None
    assert field(view, "client_uid")["default"] is None


def test_approval_view_shows_template_defaults_without_fineract_ids() -> None:
    form = t.parse_approval_form(fixture("loan_approval_template.json"))
    view = t.step_form_view(
        t.Step.APPROVAL,
        form,
        "LN-2026-000123",
        "PROBE-000001",
        "SUBMITTED_AND_PENDING_APPROVAL",
        TODAY,
    )
    no_ids(view)
    assert view["form"] == "approval"
    assert view["available"] is True
    assert view["submit_to"] == "POST /api/loans/LN-2026-000123/approve"
    approved_on = field(view, "approved_on")
    assert (approved_on["default"], approved_on["maximum"]) == ("2026-10-13", "2026-10-13")
    principal = field(view, "approved_principal")
    assert principal["read_only"] is True
    assert principal["default"] == 2_500_000
    assert view["values"]["expected_disbursement_date"] == "2026-10-20"


def test_disbursement_view_defaults_to_today_and_the_approved_amount() -> None:
    form = t.parse_disbursement_form(fixture("loan_disburse_template.json"))
    view = t.step_form_view(t.Step.DISBURSEMENT, form, "LN-2026-000123", "X", "APPROVED", TODAY)
    no_ids(view)
    assert view["available"] is True
    date_field = field(view, "transaction_date")
    assert (date_field["default"], date_field["maximum"]) == ("2026-10-13", "2026-10-13")
    assert field(view, "amount")["default"] == 2_000_000
    assert view["values"]["expected_disbursement_date"] == "2026-10-20"


def test_disbursement_view_proposes_today_after_the_expected_date_has_passed() -> None:
    form = t.parse_disbursement_form(fixture("loan_disburse_template.json"))
    later = date(2026, 10, 25)  # the template's expected date is 2026-10-20
    view = t.step_form_view(t.Step.DISBURSEMENT, form, "LN-2026-000123", "X", "APPROVED", later)
    assert field(view, "transaction_date")["default"] == "2026-10-25"
    assert view["values"]["expected_disbursement_date"] == "2026-10-20"


def test_repayment_view_lists_partner_payment_types_only() -> None:
    form = t.parse_repayment_form(fixture("loan_repayment_template.json"))
    view = t.step_form_view(t.Step.REPAYMENT, form, "LN-2026-000123", "X", "ACTIVE", TODAY)
    no_ids(view)
    assert view["submit_to"] == "POST /api/loans/LN-2026-000123/repayments"
    amount = field(view, "amount")
    assert (amount["default"], amount["minimum"], amount["required"]) == (60016, 1, True)
    assert field(view, "payment_type")["options"] == [
        {"value": "Money Transfer", "label": "Money Transfer"}
    ]
    assert view["values"] == {
        "next_due_date": "2026-11-13",
        "next_due_amount": 60016,
        "principal_portion": 60016,
        "interest_portion": 0,
        "fee_portion": 0,
        "penalty_portion": 0,
    }


def test_payoff_view_submits_to_repayments_with_the_payoff_amount() -> None:
    form = t.parse_payoff_form(fixture("loan_prepay_template.json"))
    view = t.step_form_view(t.Step.PAYOFF, form, "LN-2026-000123", "X", "ACTIVE", TODAY)
    no_ids(view)
    assert view["form"] == "payoff"
    assert view["submit_to"] == "POST /api/loans/LN-2026-000123/repayments"
    assert field(view, "amount")["default"] == 1_270_195
    assert view["values"]["payoff_amount"] == 1_270_195
    assert view["values"]["outstanding_principal"] == 1_168_000


def test_a_step_that_does_not_fit_the_status_is_shown_as_not_available() -> None:
    form = t.parse_approval_form(fixture("loan_approval_template.json"))
    view = t.step_form_view(t.Step.APPROVAL, form, "LN-2026-000123", "X", "ACTIVE", TODAY)
    assert view["available"] is False
    assert "SUBMITTED_AND_PENDING_APPROVAL" in view["message"]


# --- fetching ---------------------------------------------------------------------------------


def test_fetch_loan_application_form_reads_the_product_then_the_template() -> None:
    fake = FakeFineract(
        {
            "/loanproducts/external-id/PRD-SME-ASSET-FINANCE": {"id": 1, "name": "x"},
            "/loans/template": fixture("loan_template_individual_product.json"),
        }
    )
    form = t.fetch_loan_application_form(fake, fineract_client_id=101)
    assert form.product_id == 1
    assert fake.calls == [
        ("/loanproducts/external-id/PRD-SME-ASSET-FINANCE", None),
        ("/loans/template", {"templateType": "individual", "productId": 1, "clientId": 101}),
    ]


def test_fetch_loan_application_form_without_a_client() -> None:
    fake = FakeFineract(
        {
            "/loanproducts/external-id/PRD-SME-ASSET-FINANCE": {"id": 1},
            "/loans/template": fixture("loan_template_individual_product.json"),
        }
    )
    t.fetch_loan_application_form(fake)
    assert fake.calls[1] == ("/loans/template", {"templateType": "individual", "productId": 1})


def test_fetch_refuses_a_template_for_another_product() -> None:
    other = fixture("loan_template_individual_product.json")
    other["product"]["externalId"] = "PRD-OTHER"
    fake = FakeFineract(
        {"/loanproducts/external-id/PRD-SME-ASSET-FINANCE": {"id": 1}, "/loans/template": other}
    )
    with pytest.raises(FineractServerError):
        t.fetch_loan_application_form(fake)


@pytest.mark.parametrize(
    ("step", "path", "params", "name"),
    [
        (
            t.Step.APPROVAL,
            "/loans/external-id/LN-2026-000123/template",
            {"templateType": "approval"},
            "loan_approval_template.json",
        ),
        (
            t.Step.DISBURSEMENT,
            "/loans/external-id/LN-2026-000123/transactions/template",
            {"command": "disburse"},
            "loan_disburse_template.json",
        ),
        (
            t.Step.REPAYMENT,
            "/loans/external-id/LN-2026-000123/transactions/template",
            {"command": "repayment"},
            "loan_repayment_template.json",
        ),
        (
            t.Step.PAYOFF,
            "/loans/external-id/LN-2026-000123/transactions/template",
            {"command": "prepayLoan"},
            "loan_prepay_template.json",
        ),
    ],
)
def test_fetch_step_form_calls_the_external_id_template(
    step: t.Step, path: str, params: dict[str, str], name: str
) -> None:
    fake = FakeFineract({path: fixture(name)})
    form = t.fetch_step_form(fake, "LN-2026-000123", step)
    assert fake.calls == [(path, params)]
    kinds = {
        t.Step.APPROVAL: t.ApprovalForm,
        t.Step.DISBURSEMENT: t.DisbursementForm,
        t.Step.REPAYMENT: t.RepaymentForm,
        t.Step.PAYOFF: t.PayoffForm,
    }
    assert isinstance(form, kinds[step])


def test_fetch_passes_fineract_errors_through() -> None:
    fake = FakeFineract({"/clients/template": FineractNotFound(404, {})})
    with pytest.raises(FineractNotFound):
        t.fetch_client_form(fake)
