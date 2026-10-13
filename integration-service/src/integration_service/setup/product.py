"""The "SME Asset Finance" loan product.

build_product_body turns loan-product.json, the synced GL ids and Fineract's
GET /loanproducts/template into the body for POST /loanproducts. The template is the
source of the form, as for the partner loan flow (services/templates.py):
- loan-product.json names our business choices by code (MONTHS, DECLINING_BALANCE ...).
  Each code is resolved to its Fineract id through the template's options.
- The settings we have no business reason to set (days in year, days in month, the
  repayment start date) take the template's default.
- The strategy, the currency and each GL account are checked against the template's
  options. A GL account must be in the template's list for its role (asset, income,
  expense or liability), so a mapping to the wrong account type stops setup.
The rules that make this product what it is (one payout, cash accounting, no interest
recalculation) are fixed here, not in the config.

ensure_product never changes a product that already exists. If one with the same name has
other key settings, it raises ProductMismatchError with the differences. Loans may already use
that product, so a person must decide what to do."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from integration_service.logs import log_event
from integration_service.services.templates import FineractReader, choice_code
from integration_service.setup.steps import FineractApi, SetupError

# GL roles Fineract requires for cash-based accounting (LoanProductDataValidator, 1.15.0).
REQUIRED_CASH_ROLES = (
    "fundSourceAccountId",
    "loanPortfolioAccountId",
    "transfersInSuspenseAccountId",
    "interestOnLoanAccountId",
    "incomeFromFeeAccountId",
    "incomeFromPenaltyAccountId",
    "incomeFromRecoveryAccountId",
    "writeOffAccountId",
    "overpaymentLiabilityAccountId",
)

# GL roles Fineract accepts but does not require for cash-based accounting.
OPTIONAL_CASH_ROLES = (
    "goodwillCreditAccountId",
    "chargeOffExpenseAccountId",
    "chargeOffFraudExpenseAccountId",
    "incomeFromChargeOffInterestAccountId",
    "incomeFromChargeOffFeesAccountId",
    "incomeFromChargeOffPenaltyAccountId",
    "incomeFromGoodwillCreditInterestAccountId",
    "incomeFromGoodwillCreditFeesAccountId",
    "incomeFromGoodwillCreditPenaltyAccountId",
)

# The template's accountingMappingOptions list each GL role must pick from.
ROLE_OPTIONS = {
    "fundSourceAccountId": "assetAccountOptions",
    "loanPortfolioAccountId": "assetAccountOptions",
    "transfersInSuspenseAccountId": "assetAccountOptions",
    "interestOnLoanAccountId": "incomeAccountOptions",
    "incomeFromFeeAccountId": "incomeAccountOptions",
    "incomeFromPenaltyAccountId": "incomeAccountOptions",
    "incomeFromRecoveryAccountId": "incomeAccountOptions",
    "writeOffAccountId": "expenseAccountOptions",
    "overpaymentLiabilityAccountId": "liabilityAccountOptions",
    "goodwillCreditAccountId": "expenseAccountOptions",
    "chargeOffExpenseAccountId": "expenseAccountOptions",
    "chargeOffFraudExpenseAccountId": "expenseAccountOptions",
    "incomeFromChargeOffInterestAccountId": "incomeAccountOptions",
    "incomeFromChargeOffFeesAccountId": "incomeAccountOptions",
    "incomeFromChargeOffPenaltyAccountId": "incomeAccountOptions",
    "incomeFromGoodwillCreditInterestAccountId": "incomeAccountOptions",
    "incomeFromGoodwillCreditFeesAccountId": "incomeAccountOptions",
    "incomeFromGoodwillCreditPenaltyAccountId": "incomeAccountOptions",
}

# Our choices, by code: (config field, config key, body key). The template's
# "<body key>Options" list turns a code into its Fineract id.
CODED_CHOICES = (
    ("repayment_frequency", "repaymentFrequency", "repaymentFrequencyType"),
    ("interest_rate_frequency", "interestRateFrequency", "interestRateFrequencyType"),
    ("amortization", "amortization", "amortizationType"),
    ("interest_type", "interestType", "interestType"),
    ("interest_calculation_period", "interestCalculationPeriod", "interestCalculationPeriodType"),
)

# Not ours to choose: the body takes the template's default ("<body key>" in the template).
TEMPLATE_DEFAULTS = ("daysInYearType", "daysInMonthType", "repaymentStartDateType")

# Fixed for this product, by code, resolved through the template like the config codes.
ACCOUNTING_RULE = "CASH"
LOAN_SCHEDULE_TYPE = "CUMULATIVE"

# Fixed for this product: one payout, cash accounting, simple declining-balance schedule.
FIXED_SETTINGS: dict[str, Any] = {
    "locale": "en",
    "isLinkedToFloatingInterestRates": False,
    "allowPartialPeriodInterestCalculation": False,
    "isInterestRecalculationEnabled": False,
    "multiDisburseLoan": False,
    "includeInBorrowerCycle": False,
    "useBorrowerCycle": False,
    "canDefineInstallmentAmount": False,
    "holdGuaranteeFunds": False,
    "accountMovesOutOfNPAOnlyOnArrearsCompletion": False,
}


class LoanProductConfig(BaseModel):
    """loan-product.json: our business choices only. Plain values use Fineract's own names.
    Choices Fineract keeps as numbered options are codes here (MONTHS, YEARS ...), in the
    form services.templates.choice_code gives the template's option codes."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True
    )

    name: str = Field(min_length=1, max_length=100)
    short_name: str = Field(min_length=1, max_length=4)
    external_id: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    digits_after_decimal: int = Field(ge=0, le=6)
    in_multiples_of: int = Field(ge=0)
    installment_amount_in_multiples_of: int = Field(ge=1)
    principal: int = Field(gt=0)
    min_principal: int = Field(gt=0)
    max_principal: int = Field(gt=0)
    number_of_repayments: int = Field(gt=0)
    min_number_of_repayments: int = Field(gt=0)
    max_number_of_repayments: int = Field(gt=0)
    repayment_every: int = Field(gt=0)
    repayment_frequency: str = Field(min_length=1)
    interest_rate_per_period: int | float = Field(ge=0)
    min_interest_rate_per_period: int | float = Field(ge=0)
    max_interest_rate_per_period: int | float = Field(ge=0)
    interest_rate_frequency: str = Field(min_length=1)
    amortization: str = Field(min_length=1)
    interest_type: str = Field(min_length=1)
    interest_calculation_period: str = Field(min_length=1)
    transaction_processing_strategy_code: str = Field(min_length=1)
    gl_accounts: dict[str, str]

    @field_validator("gl_accounts")
    @classmethod
    def _known_and_complete_roles(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(REQUIRED_CASH_ROLES) - set(OPTIONAL_CASH_ROLES))
        if unknown:
            raise ValueError(f"unknown GL roles: {', '.join(unknown)}")
        missing = [role for role in REQUIRED_CASH_ROLES if role not in value]
        if missing:
            raise ValueError(f"required GL roles missing: {', '.join(missing)}")
        return value

    @model_validator(mode="after")
    def _defaults_within_limits(self) -> LoanProductConfig:
        for low, default, high, label in (
            (self.min_principal, self.principal, self.max_principal, "principal"),
            (
                self.min_number_of_repayments,
                self.number_of_repayments,
                self.max_number_of_repayments,
                "numberOfRepayments",
            ),
            (
                self.min_interest_rate_per_period,
                self.interest_rate_per_period,
                self.max_interest_rate_per_period,
                "interestRatePerPeriod",
            ),
        ):
            if not low <= default <= high:
                raise ValueError(f"{label} must be between its min and max")
        return self


class MissingGlAccountsError(SetupError):
    """Some GL roles point at ERP accounts that are not synced to Fineract yet."""

    def __init__(self, missing: dict[str, str]) -> None:
        self.missing = missing
        listed = ", ".join(f"{role} -> {ref}" for role, ref in missing.items())
        super().__init__(f"Sync these ERP accounts before the loan product: {listed}")


class ProductMismatchError(SetupError):
    """A product with this name exists with other key settings. Nothing was changed."""

    def __init__(self, product_id: int, mismatches: list[str]) -> None:
        self.product_id = product_id
        self.mismatches = mismatches
        super().__init__(
            f"Loan product {product_id} exists with other settings: {'; '.join(mismatches)}"
        )


class ProductTemplateError(SetupError):
    """The config asks for something the loan product template does not offer, or the
    template does not have the shape setup needs."""


@dataclass(frozen=True)
class ProductTemplate:
    """GET /loanproducts/template, as setup needs it.

    options: "<body key>Options" -> {our code: Fineract id}
    defaults: body key -> the template's default id, for TEMPLATE_DEFAULTS
    strategies: the transaction processing strategy codes
    currencies: the currency codes
    gl_options: accountingMappingOptions list name -> the Fineract GL ids in it"""

    options: Mapping[str, Mapping[str, int]]
    defaults: Mapping[str, int]
    strategies: tuple[str, ...]
    currencies: tuple[str, ...]
    gl_options: Mapping[str, frozenset[int]]


def _list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProductTemplateError(f"The loan product template has no {key} list")
    return value


def _coded(data: dict[str, Any], key: str) -> dict[str, int]:
    codes: dict[str, int] = {}
    for option in _list(data, key):
        code, option_id = option.get("code"), option.get("id")
        if (
            not isinstance(code, str)
            or isinstance(option_id, bool)
            or not isinstance(option_id, int)
        ):
            raise ProductTemplateError(f"The loan product template has an unreadable {key} entry")
        codes[choice_code(code)] = option_id
    return codes


def parse_product_template(data: Any) -> ProductTemplate:
    """The parts of GET /loanproducts/template setup uses. ProductTemplateError when one
    is missing or has another shape."""
    if not isinstance(data, dict):
        raise ProductTemplateError("The loan product template is not an object")
    option_keys = [f"{body_key}Options" for _, _, body_key in CODED_CHOICES]
    option_keys += [f"{key}Options" for key in TEMPLATE_DEFAULTS]
    option_keys += ["accountingRuleOptions", "loanScheduleTypeOptions"]
    options = {key: _coded(data, key) for key in option_keys}
    defaults = {}
    for key in TEMPLATE_DEFAULTS:
        default = data.get(key)
        default_id = default.get("id") if isinstance(default, dict) else None
        if not isinstance(default_id, int) or default_id not in options[f"{key}Options"].values():
            raise ProductTemplateError(f"The loan product template has no usable {key} default")
        defaults[key] = default_id
    mappings = data.get("accountingMappingOptions")
    if not isinstance(mappings, dict):
        raise ProductTemplateError("The loan product template has no accountingMappingOptions")
    gl_options = {
        name: frozenset(int(account["id"]) for account in _list(mappings, name))
        for name in sorted(set(ROLE_OPTIONS.values()))
    }
    return ProductTemplate(
        options=options,
        defaults=defaults,
        strategies=tuple(
            str(o.get("code")) for o in _list(data, "transactionProcessingStrategyOptions")
        ),
        currencies=tuple(str(o.get("code")) for o in _list(data, "currencyOptions")),
        gl_options=gl_options,
    )


def fetch_product_template(fc: FineractReader) -> ProductTemplate:
    """One read of GET /loanproducts/template."""
    return parse_product_template(fc.get("/loanproducts/template"))


def _resolve(template: ProductTemplate, options_key: str, label: str, code: str) -> int:
    allowed = template.options[options_key]
    if code not in allowed:
        raise ProductTemplateError(
            f"{label} {code!r} is not offered by Fineract. Allowed: {', '.join(allowed)}"
        )
    return allowed[code]


def _one_of(label: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ProductTemplateError(
            f"{label} {value!r} is not offered by Fineract. Allowed: {', '.join(allowed)}"
        )


def build_product_body(
    cfg: LoanProductConfig, gl_ids: Mapping[str, int], template: ProductTemplate
) -> dict[str, Any]:
    """The POST /loanproducts body. gl_ids maps an ERP reference to its Fineract GL id;
    template is the parsed GET /loanproducts/template."""
    missing = {role: ref for role, ref in cfg.gl_accounts.items() if ref not in gl_ids}
    if missing:
        raise MissingGlAccountsError(missing)
    _one_of("loan-product.json currencyCode", cfg.currency_code, template.currencies)
    _one_of(
        "loan-product.json transactionProcessingStrategyCode",
        cfg.transaction_processing_strategy_code,
        template.strategies,
    )
    wrong = [
        f"{role} -> {ref} (Fineract GL id {gl_ids[ref]}) is not in {ROLE_OPTIONS[role]}"
        for role, ref in cfg.gl_accounts.items()
        if gl_ids[ref] not in template.gl_options[ROLE_OPTIONS[role]]
    ]
    if wrong:
        raise ProductTemplateError(
            "GL accounts of the wrong type for their role: " + "; ".join(wrong)
        )
    coded = {field for field, _, _ in CODED_CHOICES}
    body = cfg.model_dump(by_alias=True, exclude={"gl_accounts", *coded})
    for field, label, body_key in CODED_CHOICES:
        body[body_key] = _resolve(
            template, f"{body_key}Options", f"loan-product.json {label}", getattr(cfg, field)
        )
    body.update(template.defaults)
    body.update(FIXED_SETTINGS)
    body["accountingRule"] = _resolve(
        template, "accountingRuleOptions", "The fixed accounting rule", ACCOUNTING_RULE
    )
    _resolve(
        template, "loanScheduleTypeOptions", "The fixed loan schedule type", LOAN_SCHEDULE_TYPE
    )
    body["loanScheduleType"] = LOAN_SCHEDULE_TYPE
    body.update({role: gl_ids[ref] for role, ref in cfg.gl_accounts.items()})
    return body


def _option_id(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda product: (product.get(key) or {}).get("id")


def _currency(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda product: (product.get("currency") or {}).get(key)


def _option_code(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda product: (product.get(key) or {}).get("code")


def _plain(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda product: product.get(key)


def _mapping(role: str) -> Callable[[dict[str, Any]], Any]:
    name = role.removesuffix("Id")
    return lambda product: ((product.get("accountingMappings") or {}).get(name) or {}).get("id")


# The key settings, as (body key, how to read it from GET /loanproducts/{id}).
_KEY_SETTINGS: tuple[tuple[str, Callable[[dict[str, Any]], Any]], ...] = (
    ("shortName", _plain("shortName")),
    ("currencyCode", _currency("code")),
    ("digitsAfterDecimal", _currency("decimalPlaces")),
    ("inMultiplesOf", _currency("inMultiplesOf")),
    ("principal", _plain("principal")),
    ("minPrincipal", _plain("minPrincipal")),
    ("maxPrincipal", _plain("maxPrincipal")),
    ("numberOfRepayments", _plain("numberOfRepayments")),
    ("repaymentEvery", _plain("repaymentEvery")),
    ("repaymentFrequencyType", _option_id("repaymentFrequencyType")),
    ("interestRatePerPeriod", _plain("interestRatePerPeriod")),
    ("interestRateFrequencyType", _option_id("interestRateFrequencyType")),
    ("amortizationType", _option_id("amortizationType")),
    ("interestType", _option_id("interestType")),
    ("interestCalculationPeriodType", _option_id("interestCalculationPeriodType")),
    ("transactionProcessingStrategyCode", _plain("transactionProcessingStrategyCode")),
    # Template defaults: compared too, so a product built on other defaults is reported.
    ("daysInYearType", _option_id("daysInYearType")),
    ("daysInMonthType", _option_id("daysInMonthType")),
    ("repaymentStartDateType", _option_id("repaymentStartDateType")),
    ("loanScheduleType", _option_code("loanScheduleType")),
    ("multiDisburseLoan", _plain("multiDisburseLoan")),
    ("accountingRule", _option_id("accountingRule")),
)


def _same(wanted: Any, have: Any) -> bool:
    numbers = (int, float, Decimal)
    if (
        isinstance(wanted, numbers)
        and isinstance(have, numbers)
        and not isinstance(wanted, bool)
        and not isinstance(have, bool)
    ):
        return Decimal(str(wanted)) == Decimal(str(have))
    return bool(wanted == have)


def product_mismatches(body: Mapping[str, Any], product: dict[str, Any]) -> list[str]:
    """How an existing product differs from the body on the settings that matter."""
    checks = list(_KEY_SETTINGS)
    checks += [
        (role, _mapping(role))
        for role in (*REQUIRED_CASH_ROLES, *OPTIONAL_CASH_ROLES)
        if role in body
    ]
    found = []
    for key, read in checks:
        wanted, have = body[key], read(product)
        if not _same(wanted, have):
            found.append(f"{key}: wanted {wanted!r}, Fineract has {have!r}")
    return found


def ensure_product(fc: FineractApi, body: dict[str, Any]) -> int:
    """The loan product's Fineract id. Creates it when no product has this name."""
    name = body["name"]
    for listed in fc.get("/loanproducts") or []:
        if listed.get("name") == name:
            product_id = int(listed["id"])
            mismatches = product_mismatches(body, fc.get(f"/loanproducts/{product_id}"))
            if mismatches:
                raise ProductMismatchError(product_id, mismatches)
            log_event("setup loan product exists", step="product", name=name)
            return product_id
    created = fc.post("/loanproducts", body)
    log_event("setup loan product created", step="product", name=name)
    return int(created["resourceId"])
