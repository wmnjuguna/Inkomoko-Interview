"""Fineract's template APIs as typed forms.

Fineract already knows the rules for each step of a loan: the product's principal range,
its term and rate, the default dates and amounts, and the payment types. Its template
calls return them. This module reads those templates, so the rules live in Fineract only
and are not copied here.

Three parts:
- parse_*: pure functions from a template's JSON to a frozen form. They raise
  FineractServerError (an unexpected answer) when the shape is wrong, like
  services.loans.parse_product does, so read_or_fail() treats it as a failed read.
- fetch_*: one Fineract read each (two for the loan application form), through anything
  with FineractClient.get(). Fineract errors pass through unchanged: wrap the call in
  read_or_fail() inside a write, or turn the error into a partner error with
  fineract_read_error() in a plain read.
- check_* and *_view: pure checks of a partner value against a form (a list of
  FieldProblem, empty when all is fine), and the partner view of a form.

Forms keep the Fineract ids that a Fineract body needs, in fields named fineract_id or
*_id. The views never show them.

The template URLs, as checked on Fineract 1.15 with the integration-api user:
- GET /clients/template
- GET /loans/template?templateType=individual&productId=..&clientId=..
- GET /loans/external-id/{ref}/template?templateType=approval
- GET /loans/external-id/{ref}/transactions/template?command=disburse|repayment|prepayLoan
A template answers 200 whatever the loan's status (an approval template for an active loan,
for example), so step_available() checks the status separately."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote

from integration_service.clients.errors import FineractServerError
from integration_service.errors import ApiError, ErrorCode
from integration_service.schemas.clients import CLIENT_UID_PATTERN
from integration_service.schemas.operations import EXTERNAL_REFERENCE_PATTERN

PRODUCT_EXTERNAL_ID = "PRD-SME-ASSET-FINANCE"
FINERACT_DATE_FORMAT = "yyyy-MM-dd"
FINERACT_LOCALE = "en"
# Fineract allows 50 characters for each client name (m_client.firstname, lastname).
NAME_MAX_LENGTH = 50
EXTERNAL_REFERENCE_MAX_LENGTH = 40
NAME_PATTERN = r"^\p{L}+(?:[ '.\-]\p{L}+)*\.?$"


class FineractReader(Protocol):
    """The one call this module needs. FineractClient and the services' FineractApi fit."""

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any: ...


class Step(StrEnum):
    """The steps of a loan after its application, as GET /api/loans/{ref}/template names them."""

    APPROVAL = "approval"
    DISBURSEMENT = "disbursement"
    REPAYMENT = "repayment"
    PAYOFF = "payoff"


# The loan status (in our form, see services.loans.loan_status_code) each step needs.
STEP_STATUS: Mapping[Step, str] = {
    Step.APPROVAL: "SUBMITTED_AND_PENDING_APPROVAL",
    Step.DISBURSEMENT: "APPROVED",
    Step.REPAYMENT: "ACTIVE",
    Step.PAYOFF: "ACTIVE",
}


# --- form types -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    decimal_places: int
    in_multiples_of: int


@dataclass(frozen=True)
class Choice:
    """One Fineract enum value. code is ours (MONTHS); label is Fineract's (Months)."""

    fineract_id: int
    code: str
    label: str


@dataclass(frozen=True)
class NamedOption:
    fineract_id: int
    name: str


@dataclass(frozen=True)
class PaymentType:
    fineract_id: int
    name: str
    system_defined: bool


@dataclass(frozen=True)
class IntRange:
    """A default with its limits. A missing limit is None (no limit)."""

    default: int | None
    minimum: int | None
    maximum: int | None

    def contains(self, value: int) -> bool:
        return (self.minimum is None or value >= self.minimum) and (
            self.maximum is None or value <= self.maximum
        )


@dataclass(frozen=True)
class RateRange:
    default: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None


@dataclass(frozen=True)
class ClientForm:
    """GET /clients/template."""

    offices: tuple[NamedOption, ...]
    default_office_id: int | None
    activation_date: date | None
    legal_forms: tuple[Choice, ...]

    def office_id(self, name: str) -> int | None:
        return next((o.fineract_id for o in self.offices if o.name == name), None)

    def legal_form_id(self, code: str) -> int | None:
        return next((c.fineract_id for c in self.legal_forms if c.code == code), None)


@dataclass(frozen=True)
class LoanApplicationForm:
    """GET /loans/template?templateType=individual&productId=..: the product's terms."""

    product_id: int
    product_external_id: str | None
    product_name: str
    product_short_name: str | None
    product_description: str | None
    currency: Currency
    principal: IntRange
    number_of_repayments: IntRange
    repayment_every: int
    repayment_frequency: Choice
    loan_term: int
    loan_term_frequency: Choice
    interest_rate_per_period: RateRange
    interest_rate_frequency: Choice
    annual_interest_rate: Decimal | None
    amortization: Choice
    interest_type: Choice
    interest_calculation_period: Choice
    transaction_processing_strategy_code: str
    expected_disbursement_date: date | None
    client_uid: str | None


@dataclass(frozen=True)
class ApprovalForm:
    """GET /loans/external-id/{ref}/template?templateType=approval. approved_on is today and
    approved_amount the applied principal, as Fineract proposes them."""

    approved_on: date | None
    approved_amount: int
    net_disbursal_amount: int | None
    expected_disbursement_date: date | None
    currency: Currency


@dataclass(frozen=True)
class DisbursementForm:
    """...transactions/template?command=disburse. expected_date is the loan's expected
    disbursement date (it can be in the future); amount is the approved principal."""

    expected_date: date | None
    amount: int
    net_disbursal_amount: int | None
    currency: Currency
    payment_types: tuple[PaymentType, ...]

    def default_date(self, today: date) -> date:
        """The date to propose: Fineract's expected date, but never later than today."""
        if self.expected_date is None or self.expected_date > today:
            return today
        return self.expected_date


@dataclass(frozen=True)
class _PaymentForm:
    payment_types: tuple[PaymentType, ...]

    @property
    def partner_payment_types(self) -> tuple[PaymentType, ...]:
        """System payment types are for Fineract's own adjustments, not for partners."""
        return tuple(p for p in self.payment_types if not p.system_defined)

    def payment_type_id(self, name: str) -> int | None:
        """A partner payment type by name, any case."""
        wanted = name.strip().casefold()
        return next(
            (p.fineract_id for p in self.partner_payment_types if p.name.casefold() == wanted),
            None,
        )


@dataclass(frozen=True)
class RepaymentForm(_PaymentForm):
    """...transactions/template?command=repayment: the next installment due."""

    due_date: date | None
    due_amount: int
    principal_portion: int
    interest_portion: int
    fee_portion: int
    penalty_portion: int
    currency: Currency


@dataclass(frozen=True)
class PayoffForm(_PaymentForm):
    """...transactions/template?command=prepayLoan: what clears the loan on on_date (today)."""

    on_date: date | None
    amount: int
    principal_portion: int
    interest_portion: int
    fee_portion: int
    penalty_portion: int
    outstanding_principal: int
    currency: Currency


type StepForm = ApprovalForm | DisbursementForm | RepaymentForm | PayoffForm

STEP_FORMS: Mapping[Step, type[StepForm]] = {
    Step.APPROVAL: ApprovalForm,
    Step.DISBURSEMENT: DisbursementForm,
    Step.REPAYMENT: RepaymentForm,
    Step.PAYOFF: PayoffForm,
}


# --- parsing ----------------------------------------------------------------------------------


class _UnreadableError(ValueError):
    pass


def choice_code(code: str) -> str:
    """'repaymentFrequency.periodFrequencyType.months' -> 'MONTHS'."""
    _, _, rest = code.partition(".")
    rest = rest or code
    rest = rest.removeprefix("periodFrequencyType.")
    return rest.replace(".", "_").upper()


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _UnreadableError("expected an object")
    return value


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _UnreadableError("expected a whole number")
    return value


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise _UnreadableError("expected a number")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise _UnreadableError("expected a number") from exc


def _amount(value: Any, currency: Currency) -> int | None:
    """A money amount in whole units. Only whole-unit currencies (RWF) are handled."""
    number = _decimal(value)
    if number is None:
        return None
    if currency.decimal_places != 0 or number != number.to_integral_value():
        raise _UnreadableError("expected a whole amount")
    return int(number)


def _required_amount(value: Any, currency: Currency) -> int:
    amount = _amount(value, currency)
    return 0 if amount is None else amount


def _date(value: Any) -> date | None:
    """Fineract sends dates as [year, month, day]."""
    if value is None:
        return None
    if isinstance(value, list) and len(value) == 3 and all(isinstance(v, int) for v in value):
        try:
            return date(value[0], value[1], value[2])
        except ValueError as exc:
            raise _UnreadableError("bad date") from exc
    raise _UnreadableError("expected a date")


def _choice(value: Any) -> Choice:
    data = _dict(value)
    code = data.get("code")
    if not isinstance(code, str):
        raise _UnreadableError("expected an enum code")
    return Choice(_int(data.get("id")), choice_code(code), str(data.get("value") or ""))


def _currency(value: Any) -> Currency:
    data = _dict(value)
    return Currency(
        code=str(data["code"]),
        name=str(data.get("name") or data["code"]),
        decimal_places=_int(data.get("decimalPlaces", 0)),
        in_multiples_of=_int(data.get("inMultiplesOf") or 1),
    )


def _payment_types(value: Any) -> tuple[PaymentType, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _UnreadableError("expected a list of payment types")
    return tuple(
        PaymentType(
            _int(_dict(item).get("id")),
            str(item.get("name") or ""),
            bool(item.get("isSystemDefined")),
        )
        for item in value
    )


def _whole_count(value: Any) -> int | None:
    number = _decimal(value)
    return None if number is None else int(number)


def _parsed[T](what: str, data: Any, build: Callable[[dict[str, Any]], T]) -> T:
    try:
        result = build(_dict(data))
    except (_UnreadableError, KeyError, TypeError, ValueError) as exc:
        body = data if isinstance(data, dict) else {}
        raise FineractServerError(200, body, f"Unexpected Fineract {what} template") from exc
    return result


def parse_client_form(data: Any) -> ClientForm:
    def build(d: dict[str, Any]) -> ClientForm:
        offices = tuple(
            NamedOption(_int(_dict(o).get("id")), str(o.get("name") or ""))
            for o in d.get("officeOptions") or []
        )
        office = d.get("officeId")
        return ClientForm(
            offices=offices,
            default_office_id=_int(office) if office is not None else None,
            activation_date=_date(d.get("activationDate")),
            legal_forms=tuple(_choice(c) for c in d.get("clientLegalFormOptions") or []),
        )

    return _parsed("client", data, build)


def parse_loan_application_form(data: Any) -> LoanApplicationForm:
    def build(d: dict[str, Any]) -> LoanApplicationForm:
        product = _dict(d["product"])
        currency = _currency(d.get("currency") or product["currency"])
        return LoanApplicationForm(
            product_id=_int(product["id"]),
            product_external_id=product.get("externalId"),
            product_name=str(product["name"]),
            product_short_name=product.get("shortName"),
            product_description=product.get("description"),
            currency=currency,
            principal=IntRange(
                default=_amount(d.get("principal", product.get("principal")), currency),
                minimum=_amount(product.get("minPrincipal"), currency),
                maximum=_amount(product.get("maxPrincipal"), currency),
            ),
            number_of_repayments=IntRange(
                default=_whole_count(d.get("numberOfRepayments")),
                minimum=_whole_count(product.get("minNumberOfRepayments")),
                maximum=_whole_count(product.get("maxNumberOfRepayments")),
            ),
            repayment_every=_int(d["repaymentEvery"]),
            repayment_frequency=_choice(d["repaymentFrequencyType"]),
            loan_term=_int(d["termFrequency"]),
            loan_term_frequency=_choice(d["termPeriodFrequencyType"]),
            interest_rate_per_period=RateRange(
                default=_decimal(d.get("interestRatePerPeriod")),
                minimum=_decimal(product.get("minInterestRatePerPeriod")),
                maximum=_decimal(product.get("maxInterestRatePerPeriod")),
            ),
            interest_rate_frequency=_choice(d["interestRateFrequencyType"]),
            annual_interest_rate=_decimal(d.get("annualInterestRate")),
            amortization=_choice(d["amortizationType"]),
            interest_type=_choice(d["interestType"]),
            interest_calculation_period=_choice(d["interestCalculationPeriodType"]),
            transaction_processing_strategy_code=str(d["transactionProcessingStrategyCode"]),
            expected_disbursement_date=_date(d.get("expectedDisbursementDate")),
            client_uid=d.get("clientExternalId"),
        )

    return _parsed("loan application", data, build)


def parse_approval_form(data: Any) -> ApprovalForm:
    def build(d: dict[str, Any]) -> ApprovalForm:
        currency = _currency(d["currency"])
        return ApprovalForm(
            approved_on=_date(d.get("approvalDate")),
            approved_amount=_required_amount(d.get("approvalAmount"), currency),
            net_disbursal_amount=_amount(d.get("netDisbursalAmount"), currency),
            expected_disbursement_date=_date(d.get("expectedDisbursementDate")),
            currency=currency,
        )

    return _parsed("approval", data, build)


def parse_disbursement_form(data: Any) -> DisbursementForm:
    def build(d: dict[str, Any]) -> DisbursementForm:
        currency = _currency(d["currency"])
        return DisbursementForm(
            expected_date=_date(d.get("date")),
            amount=_required_amount(d.get("amount"), currency),
            net_disbursal_amount=_amount(d.get("netDisbursalAmount"), currency),
            currency=currency,
            payment_types=_payment_types(d.get("paymentTypeOptions")),
        )

    return _parsed("disbursement", data, build)


def parse_repayment_form(data: Any) -> RepaymentForm:
    def build(d: dict[str, Any]) -> RepaymentForm:
        currency = _currency(d["currency"])
        return RepaymentForm(
            payment_types=_payment_types(d.get("paymentTypeOptions")),
            due_date=_date(d.get("date")),
            due_amount=_required_amount(d.get("amount"), currency),
            principal_portion=_required_amount(d.get("principalPortion"), currency),
            interest_portion=_required_amount(d.get("interestPortion"), currency),
            fee_portion=_required_amount(d.get("feeChargesPortion"), currency),
            penalty_portion=_required_amount(d.get("penaltyChargesPortion"), currency),
            currency=currency,
        )

    return _parsed("repayment", data, build)


def parse_payoff_form(data: Any) -> PayoffForm:
    def build(d: dict[str, Any]) -> PayoffForm:
        currency = _currency(d["currency"])
        return PayoffForm(
            payment_types=_payment_types(d.get("paymentTypeOptions")),
            on_date=_date(d.get("date")),
            amount=_required_amount(d.get("amount"), currency),
            principal_portion=_required_amount(d.get("principalPortion"), currency),
            interest_portion=_required_amount(d.get("interestPortion"), currency),
            fee_portion=_required_amount(d.get("feeChargesPortion"), currency),
            penalty_portion=_required_amount(d.get("penaltyChargesPortion"), currency),
            outstanding_principal=_required_amount(d.get("outstandingLoanBalance"), currency),
            currency=currency,
        )

    return _parsed("payoff", data, build)


# --- fetching ---------------------------------------------------------------------------------


def _loan_path(loan_reference: str) -> str:
    return f"/loans/external-id/{quote(loan_reference, safe='')}"


def fetch_client_form(fineract: FineractReader, correlation_id: str | None = None) -> ClientForm:
    return parse_client_form(fineract.get("/clients/template", correlation_id=correlation_id))


def fetch_product_id(
    fineract: FineractReader,
    product_external_id: str = PRODUCT_EXTERNAL_ID,
    correlation_id: str | None = None,
) -> int:
    data = fineract.get(
        f"/loanproducts/external-id/{quote(product_external_id, safe='')}",
        correlation_id=correlation_id,
    )
    product_id = data.get("id") if isinstance(data, dict) else None
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        body = data if isinstance(data, dict) else {}
        raise FineractServerError(200, body, "Unexpected Fineract loan product")
    return product_id


def fetch_loan_application_form(
    fineract: FineractReader,
    fineract_client_id: int | None = None,
    product_external_id: str = PRODUCT_EXTERNAL_ID,
    correlation_id: str | None = None,
) -> LoanApplicationForm:
    """The product's id by its externalId, then the individual loan template for it (and
    for the client, when given: then the template carries the client's defaults too)."""
    product_id = fetch_product_id(fineract, product_external_id, correlation_id)
    params: dict[str, Any] = {"templateType": "individual", "productId": product_id}
    if fineract_client_id is not None:
        params["clientId"] = fineract_client_id
    data = fineract.get("/loans/template", params=params, correlation_id=correlation_id)
    form = parse_loan_application_form(data)
    if form.product_external_id != product_external_id:
        raise FineractServerError(200, {}, "Fineract sent the template of another product")
    return form


def fetch_approval_form(
    fineract: FineractReader, loan_reference: str, correlation_id: str | None = None
) -> ApprovalForm:
    data = fineract.get(
        f"{_loan_path(loan_reference)}/template",
        params={"templateType": "approval"},
        correlation_id=correlation_id,
    )
    return parse_approval_form(data)


def _transaction_template(
    fineract: FineractReader, loan_reference: str, command: str, correlation_id: str | None
) -> Any:
    return fineract.get(
        f"{_loan_path(loan_reference)}/transactions/template",
        params={"command": command},
        correlation_id=correlation_id,
    )


def fetch_disbursement_form(
    fineract: FineractReader, loan_reference: str, correlation_id: str | None = None
) -> DisbursementForm:
    return parse_disbursement_form(
        _transaction_template(fineract, loan_reference, "disburse", correlation_id)
    )


def fetch_repayment_form(
    fineract: FineractReader, loan_reference: str, correlation_id: str | None = None
) -> RepaymentForm:
    return parse_repayment_form(
        _transaction_template(fineract, loan_reference, "repayment", correlation_id)
    )


def fetch_payoff_form(
    fineract: FineractReader, loan_reference: str, correlation_id: str | None = None
) -> PayoffForm:
    return parse_payoff_form(
        _transaction_template(fineract, loan_reference, "prepayLoan", correlation_id)
    )


def fetch_step_form(
    fineract: FineractReader, loan_reference: str, step: Step, correlation_id: str | None = None
) -> StepForm:
    if step is Step.APPROVAL:
        return fetch_approval_form(fineract, loan_reference, correlation_id)
    if step is Step.DISBURSEMENT:
        return fetch_disbursement_form(fineract, loan_reference, correlation_id)
    if step is Step.REPAYMENT:
        return fetch_repayment_form(fineract, loan_reference, correlation_id)
    return fetch_payoff_form(fineract, loan_reference, correlation_id)


# --- the Fineract body ------------------------------------------------------------------------


def build_application_body(
    form: LoanApplicationForm,
    fineract_client_id: int,
    principal: int,
    loan_reference: str,
    submitted_on: date,
    expected_disbursement_on: date,
) -> dict[str, Any]:
    """POST /loans with the template's own terms. Only principal and the dates are ours."""
    return {
        "clientId": fineract_client_id,
        "productId": form.product_id,
        "principal": principal,
        "loanTermFrequency": form.loan_term,
        "loanTermFrequencyType": form.loan_term_frequency.fineract_id,
        "numberOfRepayments": form.number_of_repayments.default,
        "repaymentEvery": form.repayment_every,
        "repaymentFrequencyType": form.repayment_frequency.fineract_id,
        "interestRatePerPeriod": _float(form.interest_rate_per_period.default),
        "amortizationType": form.amortization.fineract_id,
        "interestType": form.interest_type.fineract_id,
        "interestCalculationPeriodType": form.interest_calculation_period.fineract_id,
        "transactionProcessingStrategyCode": form.transaction_processing_strategy_code,
        "expectedDisbursementDate": expected_disbursement_on.isoformat(),
        "submittedOnDate": submitted_on.isoformat(),
        "loanType": "individual",
        "externalId": loan_reference,
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": FINERACT_LOCALE,
    }


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


# --- checks -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldProblem:
    """One field that does not fit the form. The shape of details.problems[] in a
    VALIDATION_ERROR answer."""

    field: str
    problem: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "problem": self.problem}


def validation_error(problems: list[FieldProblem]) -> ApiError:
    """422 VALIDATION_ERROR naming the first problem, all of them in details.problems."""
    first = problems[0] if problems else FieldProblem("body", "invalid")
    return ApiError(
        ErrorCode.VALIDATION_ERROR,
        f"{first.field}: {first.problem}",
        422,
        details={"problems": [p.as_dict() for p in problems]},
    )


def _check_amount(
    name: str, value: int, limits: IntRange, currency: Currency, what: str
) -> list[FieldProblem]:
    if not limits.contains(value):
        if limits.minimum is not None and limits.maximum is not None:
            text = f"must be between {limits.minimum} and {limits.maximum} {currency.code}"
        elif limits.minimum is not None:
            text = f"must be at least {limits.minimum} {currency.code}"
        else:
            text = f"must be at most {limits.maximum} {currency.code}"
        return [FieldProblem(name, f"{text}{what}")]
    step = currency.in_multiples_of
    if step > 1 and value % step:
        return [FieldProblem(name, f"must be a multiple of {step} {currency.code}")]
    return []


def _not_future(name: str, value: date | None, today: date) -> list[FieldProblem]:
    if value is not None and value > today:
        return [FieldProblem(name, "cannot be in the future (Kigali date)")]
    return []


def check_application(
    form: LoanApplicationForm,
    principal: int,
    expected_disbursement_date: date | None,
    today: date,
) -> list[FieldProblem]:
    """principal within the product's range (and currency step); the date not in the past."""
    problems = _check_amount(
        "principal", principal, form.principal, form.currency, " (the product range)"
    )
    if expected_disbursement_date is not None and expected_disbursement_date < today:
        problems.append(
            FieldProblem("expected_disbursement_date", "cannot be in the past (Kigali date)")
        )
    return problems


def check_approval(
    form: ApprovalForm,
    approved_on: date | None,
    today: date,
    approved_principal: int | None = None,
) -> list[FieldProblem]:
    """approved_on not in the future; an approved amount no more than Fineract proposes."""
    problems = _not_future("approved_on", approved_on, today)
    if approved_principal is not None:
        problems += _check_amount(
            "approved_principal",
            approved_principal,
            IntRange(None, 1, form.approved_amount),
            form.currency,
            " (the applied principal)",
        )
    return problems


def check_disbursement(
    form: DisbursementForm, transaction_date: date | None, today: date
) -> list[FieldProblem]:
    return _not_future("transaction_date", transaction_date, today)


def check_repayment(
    form: RepaymentForm | PayoffForm,
    amount: int,
    transaction_date: date | None,
    payment_type: str | None,
    today: date,
) -> list[FieldProblem]:
    """amount above zero (in the currency step), the date not in the future, and the
    payment type one of the partner payment types Fineract lists."""
    problems = _check_amount("amount", amount, IntRange(None, 1, None), form.currency, "")
    problems += _not_future("transaction_date", transaction_date, today)
    if payment_type is not None and form.payment_type_id(payment_type) is None:
        names = ", ".join(p.name for p in form.partner_payment_types) or "none"
        problems.append(FieldProblem("payment_type", f"must be one of: {names}"))
    return problems


def step_available(step: Step, loan_status: str) -> bool:
    return STEP_STATUS[step] == loan_status


# --- partner views ----------------------------------------------------------------------------


def _field(
    name: str,
    kind: str,
    description: str,
    *,
    required: bool = False,
    read_only: bool = False,
    default: Any = None,
    minimum: Any = None,
    maximum: Any = None,
    multiple_of: int | None = None,
    pattern: str | None = None,
    max_length: int | None = None,
    options: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": kind,
        "required": required,
        "read_only": read_only,
        "default": default,
        "minimum": minimum,
        "maximum": maximum,
        "multiple_of": multiple_of,
        "pattern": pattern,
        "max_length": max_length,
        "options": options,
        "description": description,
    }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _currency_view(currency: Currency) -> dict[str, Any]:
    return {
        "code": currency.code,
        "name": currency.name,
        "decimal_places": currency.decimal_places,
        "in_multiples_of": currency.in_multiples_of,
    }


def _choice_field(name: str, choice: Choice, description: str) -> dict[str, Any]:
    return _field(
        name,
        "string",
        description,
        read_only=True,
        default=choice.code,
        options=[{"value": choice.code, "label": choice.label}],
    )


def _external_reference_field() -> dict[str, Any]:
    return _field(
        "external_reference",
        "string",
        "Your key for this request. Send the same value on every retry.",
        required=True,
        pattern=EXTERNAL_REFERENCE_PATTERN,
        max_length=EXTERNAL_REFERENCE_MAX_LENGTH,
    )


def client_form_view(form: ClientForm) -> dict[str, Any]:
    """The client form for partners. The office and activation date come from Fineract."""
    office = next((o.name for o in form.offices if o.fineract_id == form.default_office_id), None)
    return {
        "form": "client",
        "submit_to": "POST /api/clients",
        "office": office,
        "activation_date": _iso(form.activation_date),
        "fields": [
            _external_reference_field(),
            _field(
                "client_uid",
                "string",
                "Your business id for the client, e.g. SME-000001. Unique. Fineract keeps it "
                "as the client's externalId.",
                required=True,
                pattern=CLIENT_UID_PATTERN,
            ),
            _field(
                "first_name",
                "string",
                "Letters, single spaces, apostrophes, dots or hyphens.",
                required=True,
                pattern=NAME_PATTERN,
                max_length=NAME_MAX_LENGTH,
            ),
            _field(
                "last_name",
                "string",
                "Letters, single spaces, apostrophes, dots or hyphens.",
                required=True,
                pattern=NAME_PATTERN,
                max_length=NAME_MAX_LENGTH,
            ),
        ],
    }


def loan_application_form_view(
    form: LoanApplicationForm, client_uid: str | None, today: date
) -> dict[str, Any]:
    """The loan application form for partners: what to send, and the product's terms
    (read only: they come from the product)."""
    code = form.currency.code
    rate = form.interest_rate_per_period
    return {
        "form": "loan_application",
        "submit_to": "POST /api/loans",
        "client_uid": client_uid,
        "product": {
            "name": form.product_name,
            "short_name": form.product_short_name,
            "description": form.product_description,
            "currency": _currency_view(form.currency),
        },
        "fields": [
            _external_reference_field(),
            _field(
                "client_uid",
                "string",
                "A client created through POST /api/clients.",
                required=True,
                default=client_uid,
                pattern=CLIENT_UID_PATTERN,
            ),
            _field(
                "principal",
                "integer",
                f"Whole {code}, within the product's range.",
                required=True,
                default=form.principal.default,
                minimum=form.principal.minimum,
                maximum=form.principal.maximum,
                multiple_of=form.currency.in_multiples_of,
            ),
            _field(
                "expected_disbursement_date",
                "date",
                "YYYY-MM-DD, today or later (Kigali time). Default: Fineract's proposal.",
                default=_iso(form.expected_disbursement_date),
                minimum=today.isoformat(),
            ),
            _field(
                "number_of_repayments",
                "integer",
                "Set by the product.",
                read_only=True,
                default=form.number_of_repayments.default,
                minimum=form.number_of_repayments.minimum,
                maximum=form.number_of_repayments.maximum,
            ),
            _field(
                "repayment_every",
                "integer",
                "Installments come every this many repayment_frequency units.",
                read_only=True,
                default=form.repayment_every,
            ),
            _choice_field("repayment_frequency", form.repayment_frequency, "Set by the product."),
            _field(
                "loan_term",
                "integer",
                "The loan's term, in loan_term_frequency units. Set by the product.",
                read_only=True,
                default=form.loan_term,
            ),
            _choice_field("loan_term_frequency", form.loan_term_frequency, "Set by the product."),
            _field(
                "interest_rate_per_period",
                "number",
                "Percent, per interest_rate_frequency. Set by the product.",
                read_only=True,
                default=_number(rate.default),
                minimum=_number(rate.minimum),
                maximum=_number(rate.maximum),
            ),
            _choice_field(
                "interest_rate_frequency", form.interest_rate_frequency, "Set by the product."
            ),
            _choice_field("amortization", form.amortization, "Set by the product."),
            _choice_field("interest_type", form.interest_type, "Set by the product."),
            _choice_field(
                "interest_calculation_period",
                form.interest_calculation_period,
                "Set by the product.",
            ),
        ],
    }


def _payment_type_field(form: RepaymentForm | PayoffForm) -> dict[str, Any]:
    return _field(
        "payment_type",
        "string",
        "Optional. One of the payment types Fineract lists.",
        options=[{"value": p.name, "label": p.name} for p in form.partner_payment_types],
    )


def _date_field(name: str, default: date | None, today: date, text: str) -> dict[str, Any]:
    return _field(
        name,
        "date",
        f"YYYY-MM-DD, not in the future (Kigali time). {text}",
        default=_iso(default),
        maximum=today.isoformat(),
    )


def _step_body(
    form: StepForm, loan_reference: str, today: date
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """submit_to, fields and values for one step form."""
    loan = f"/api/loans/{loan_reference}"
    code = form.currency.code
    if isinstance(form, ApprovalForm):
        return (
            f"POST {loan}/approve",
            [
                _external_reference_field(),
                _date_field("approved_on", form.approved_on, today, "Default: today."),
                _field(
                    "approved_principal",
                    "integer",
                    f"Whole {code}. The loan is approved for the applied principal.",
                    read_only=True,
                    default=form.approved_amount,
                ),
            ],
            {
                "approved_principal": form.approved_amount,
                "expected_disbursement_date": _iso(form.expected_disbursement_date),
            },
        )
    if isinstance(form, DisbursementForm):
        return (
            f"POST {loan}/disburse",
            [
                _external_reference_field(),
                _date_field(
                    "transaction_date",
                    form.default_date(today),
                    today,
                    "Default: the expected disbursement date, or today if that is later.",
                ),
                _field(
                    "amount",
                    "integer",
                    f"Whole {code}. The full approved principal is paid out.",
                    read_only=True,
                    default=form.amount,
                ),
            ],
            {
                "amount": form.amount,
                "expected_disbursement_date": _iso(form.expected_date),
            },
        )
    if isinstance(form, RepaymentForm):
        return (
            f"POST {loan}/repayments",
            [
                _external_reference_field(),
                _field(
                    "amount",
                    "integer",
                    f"Whole {code}. Default: the next installment Fineract shows.",
                    required=True,
                    default=form.due_amount,
                    minimum=1,
                    multiple_of=form.currency.in_multiples_of,
                ),
                _date_field("transaction_date", today, today, "Default: today."),
                _payment_type_field(form),
            ],
            {
                "next_due_date": _iso(form.due_date),
                "next_due_amount": form.due_amount,
                "principal_portion": form.principal_portion,
                "interest_portion": form.interest_portion,
                "fee_portion": form.fee_portion,
                "penalty_portion": form.penalty_portion,
            },
        )
    return (
        f"POST {loan}/repayments",
        [
            _external_reference_field(),
            _field(
                "amount",
                "integer",
                f"Whole {code}. Default: what clears the loan today, from Fineract.",
                required=True,
                default=form.amount,
                minimum=1,
                multiple_of=form.currency.in_multiples_of,
            ),
            _date_field("transaction_date", today, today, "Default: today."),
            _payment_type_field(form),
        ],
        {
            "payoff_date": _iso(form.on_date),
            "payoff_amount": form.amount,
            "principal_portion": form.principal_portion,
            "interest_portion": form.interest_portion,
            "fee_portion": form.fee_portion,
            "penalty_portion": form.penalty_portion,
            "outstanding_principal": form.outstanding_principal,
        },
    )


def step_form_view(
    step: Step,
    form: StepForm,
    loan_reference: str,
    client_uid: str | None,
    loan_status: str,
    today: date,
) -> dict[str, Any]:
    """The form for one step of one loan. available is False when the loan's status does
    not allow the step now (Fineract's template does not check that)."""
    submit_to, fields, values = _step_body(form, loan_reference, today)
    available = step_available(step, loan_status)
    message = None
    if not available:
        message = (
            f"The {step.value} step needs the loan to be {STEP_STATUS[step]}; "
            f"Fineract shows it {loan_status}."
        )
    return {
        "form": step.value,
        "loan_reference": loan_reference,
        "client_uid": client_uid,
        "loan_status": loan_status,
        "available": available,
        "message": message,
        "submit_to": submit_to,
        "currency": _currency_view(form.currency),
        "fields": fields,
        "values": values,
    }
