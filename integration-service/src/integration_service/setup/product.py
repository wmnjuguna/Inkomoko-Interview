"""The "SME Asset Finance" loan product.

build_product_body turns loan-product.json and the synced GL ids into the body for
POST /loanproducts. The rules that make this product what it is (one payout, cash
accounting, no interest recalculation) are fixed here, not in the config.

ensure_product never changes a product that already exists. If one with the same name has
other key settings, it raises ProductMismatchError with the differences. Loans may already use
that product, so a person must decide what to do."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from integration_service.logs import log_event
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

CASH_BASED = 2

# Fixed for this product: one payout, cash accounting, simple declining-balance schedule.
FIXED_SETTINGS: dict[str, Any] = {
    "locale": "en",
    "loanScheduleType": "CUMULATIVE",
    "isLinkedToFloatingInterestRates": False,
    "allowPartialPeriodInterestCalculation": False,
    "isInterestRecalculationEnabled": False,
    "multiDisburseLoan": False,
    "includeInBorrowerCycle": False,
    "useBorrowerCycle": False,
    "canDefineInstallmentAmount": False,
    "holdGuaranteeFunds": False,
    "accountMovesOutOfNPAOnlyOnArrearsCompletion": False,
    "accountingRule": CASH_BASED,
}


class LoanProductConfig(BaseModel):
    """loan-product.json. The keys are Fineract's own names, so the file reads like the body."""

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
    repayment_frequency_type: int
    interest_rate_per_period: int | float = Field(ge=0)
    min_interest_rate_per_period: int | float = Field(ge=0)
    max_interest_rate_per_period: int | float = Field(ge=0)
    interest_rate_frequency_type: int
    amortization_type: int
    interest_type: int
    interest_calculation_period_type: int
    transaction_processing_strategy_code: str = Field(min_length=1)
    days_in_year_type: int
    days_in_month_type: int
    repayment_start_date_type: int
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


def build_product_body(cfg: LoanProductConfig, gl_ids: Mapping[str, int]) -> dict[str, Any]:
    """The POST /loanproducts body. gl_ids maps an ERP reference to its Fineract GL id."""
    missing = {role: ref for role, ref in cfg.gl_accounts.items() if ref not in gl_ids}
    if missing:
        raise MissingGlAccountsError(missing)
    body = cfg.model_dump(by_alias=True, exclude={"gl_accounts"})
    body.update(FIXED_SETTINGS)
    body.update({role: gl_ids[ref] for role, ref in cfg.gl_accounts.items()})
    return body


def _option_id(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda product: (product.get(key) or {}).get("id")


def _currency(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda product: (product.get("currency") or {}).get(key)


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
