"""The loan product: the body built from config and synced GL ids, and ensure_product."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from integration_service.setup.config_files import load_setup_config
from integration_service.setup.product import (
    REQUIRED_CASH_ROLES,
    LoanProductConfig,
    MissingGlAccountsError,
    ProductMismatchError,
    build_product_body,
    ensure_product,
    product_mismatches,
)

# Made-up Fineract ids, one per ERP reference in loan-product.json.
GL_IDS = {
    "GL-1100": 11,
    "GL-1200": 12,
    "GL-1300": 13,
    "GL-2100": 21,
    "GL-4100": 41,
    "GL-4200": 42,
    "GL-4300": 43,
    "GL-4400": 44,
    "GL-5100": 51,
}


@pytest.fixture
def cfg() -> LoanProductConfig:
    return load_setup_config().loan_product


def test_body_has_every_field_from_config_and_the_fixed_rules(cfg: LoanProductConfig) -> None:
    body = build_product_body(cfg, GL_IDS)
    assert body == {
        "locale": "en",
        "name": "SME Asset Finance",
        "shortName": "SMEA",
        "externalId": "PRD-SME-ASSET-FINANCE",
        "description": (
            "SME asset finance: 18% a year, declining balance, 12 monthly equal installments, "
            "paid out once, RWF."
        ),
        "currencyCode": "RWF",
        "digitsAfterDecimal": 0,
        "inMultiplesOf": 1,
        "installmentAmountInMultiplesOf": 1,
        "principal": 5000000,
        "minPrincipal": 100000,
        "maxPrincipal": 50000000,
        "numberOfRepayments": 12,
        "minNumberOfRepayments": 12,
        "maxNumberOfRepayments": 12,
        "repaymentEvery": 1,
        "repaymentFrequencyType": 2,
        "interestRatePerPeriod": 18,
        "minInterestRatePerPeriod": 18,
        "maxInterestRatePerPeriod": 18,
        "interestRateFrequencyType": 3,
        "amortizationType": 1,
        "interestType": 0,
        "interestCalculationPeriodType": 1,
        "transactionProcessingStrategyCode": "mifos-standard-strategy",
        "daysInYearType": 1,
        "daysInMonthType": 1,
        "repaymentStartDateType": 1,
        # Fixed in code: single payout, cash accounting, no recalculation.
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
        "accountingRule": 2,
        "fundSourceAccountId": 11,
        "loanPortfolioAccountId": 12,
        "transfersInSuspenseAccountId": 13,
        "interestOnLoanAccountId": 41,
        "incomeFromFeeAccountId": 42,
        "incomeFromPenaltyAccountId": 43,
        "incomeFromRecoveryAccountId": 44,
        "writeOffAccountId": 51,
        "overpaymentLiabilityAccountId": 21,
    }


def test_body_never_carries_keys_fineract_refuses_for_this_product(
    cfg: LoanProductConfig,
) -> None:
    body = build_product_body(cfg, GL_IDS)
    for key in ("chargeOffBehaviour", "supportedInterestRefundTypes", "fixedLength"):
        assert key not in body
    assert "enableAccrualActivityPosting" not in body
    # Older Fineract names that 1.15.0 refuses as unsupported parameters (seen on a real run).
    for key in ("allowPartialPeriodInterestCalcualtion", "canDefineEmiAmount"):
        assert key not in body


def test_every_required_cash_role_is_mapped_in_the_config(cfg: LoanProductConfig) -> None:
    assert set(cfg.gl_accounts) == set(REQUIRED_CASH_ROLES)
    assert len(REQUIRED_CASH_ROLES) == 9


def test_missing_synced_account_is_refused_with_the_roles_named(cfg: LoanProductConfig) -> None:
    ids = {ref: gl_id for ref, gl_id in GL_IDS.items() if ref not in ("GL-1200", "GL-5100")}
    with pytest.raises(MissingGlAccountsError) as caught:
        build_product_body(cfg, ids)
    assert caught.value.missing == {
        "loanPortfolioAccountId": "GL-1200",
        "writeOffAccountId": "GL-5100",
    }
    assert "GL-1200" in str(caught.value)


def _raw_config() -> dict[str, Any]:
    return load_setup_config().loan_product.model_dump(by_alias=True)


def test_config_without_a_required_role_is_refused() -> None:
    raw = _raw_config()
    del raw["glAccounts"]["writeOffAccountId"]
    with pytest.raises(ValidationError, match="writeOffAccountId"):
        LoanProductConfig.model_validate(raw)


def test_config_with_an_unknown_role_or_key_is_refused() -> None:
    raw = _raw_config()
    raw["glAccounts"]["madeUpAccountId"] = "GL-9999"
    with pytest.raises(ValidationError, match="madeUpAccountId"):
        LoanProductConfig.model_validate(raw)
    raw = _raw_config()
    raw["chargeOffBehaviour"] = "REGULAR"
    with pytest.raises(ValidationError, match="chargeOffBehaviour"):
        LoanProductConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("shortName", "SMEAF"),  # Fineract allows 4 characters
        ("digitsAfterDecimal", 7),
        ("principal", 1),  # below minPrincipal
        ("numberOfRepayments", 24),  # above maxNumberOfRepayments
        ("interestRatePerPeriod", 30),  # above maxInterestRatePerPeriod
    ],
)
def test_config_values_are_checked(key: str, value: Any) -> None:
    raw = _raw_config()
    raw[key] = value
    with pytest.raises(ValidationError):
        LoanProductConfig.model_validate(raw)


# --- ensure_product ------------------------------------------------------------------------


class FakeFineract:
    """Holds loan products in memory and records each write."""

    def __init__(self, products: list[dict[str, Any]] | None = None) -> None:
        self.products = products or []
        self.writes: list[tuple[str, str, Any]] = []

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        if path == "/loanproducts":
            return [{"id": p["id"], "name": p["name"]} for p in self.products]
        for product in self.products:
            if path == f"/loanproducts/{product['id']}":
                return product
        raise AssertionError(f"unexpected GET {path}")

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.writes.append(("POST", path, body))
        return {"resourceId": 77}

    def put(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.writes.append(("PUT", path, body))
        return {}


def existing_product(body: dict[str, Any], product_id: int = 5) -> dict[str, Any]:
    """The product as Fineract's GET /loanproducts/{id} shows it, for a body it accepted."""
    mappings = {
        role.removesuffix("Id"): {"id": body[role], "name": "x", "glCode": "x"}
        for role in REQUIRED_CASH_ROLES
    }
    return {
        "id": product_id,
        "name": body["name"],
        "shortName": body["shortName"],
        "externalId": body["externalId"],
        "currency": {
            "code": body["currencyCode"],
            "decimalPlaces": body["digitsAfterDecimal"],
            "inMultiplesOf": body["inMultiplesOf"],
        },
        "principal": float(body["principal"]),
        "minPrincipal": float(body["minPrincipal"]),
        "maxPrincipal": float(body["maxPrincipal"]),
        "numberOfRepayments": body["numberOfRepayments"],
        "repaymentEvery": body["repaymentEvery"],
        "repaymentFrequencyType": {"id": body["repaymentFrequencyType"]},
        "interestRatePerPeriod": float(body["interestRatePerPeriod"]),
        "interestRateFrequencyType": {"id": body["interestRateFrequencyType"]},
        "amortizationType": {"id": body["amortizationType"]},
        "interestType": {"id": body["interestType"]},
        "interestCalculationPeriodType": {"id": body["interestCalculationPeriodType"]},
        "transactionProcessingStrategyCode": body["transactionProcessingStrategyCode"],
        "multiDisburseLoan": False,
        "accountingRule": {"id": 2, "code": "accountingRuleType.cash", "value": "CASH BASED"},
        "accountingMappings": mappings,
    }


def test_ensure_product_creates_it_when_missing(cfg: LoanProductConfig) -> None:
    body = build_product_body(cfg, GL_IDS)
    fake = FakeFineract([{"id": 3, "name": "Other product"}])
    assert ensure_product(fake, body) == 77
    assert fake.writes == [("POST", "/loanproducts", body)]


def test_ensure_product_returns_the_existing_id_and_writes_nothing(cfg: LoanProductConfig) -> None:
    body = build_product_body(cfg, GL_IDS)
    fake = FakeFineract([existing_product(body)])
    assert ensure_product(fake, body) == 5
    assert fake.writes == []


def test_existing_product_with_other_settings_is_reported_not_changed(
    cfg: LoanProductConfig,
) -> None:
    body = build_product_body(cfg, GL_IDS)
    product = existing_product(body)
    product["interestRatePerPeriod"] = 20.0
    product["currency"]["code"] = "USD"
    product["multiDisburseLoan"] = True
    product["accountingMappings"]["loanPortfolioAccount"]["id"] = 99
    fake = FakeFineract([product])
    with pytest.raises(ProductMismatchError) as caught:
        ensure_product(fake, body)
    assert fake.writes == []
    assert caught.value.product_id == 5
    assert caught.value.mismatches == [
        "currencyCode: wanted 'RWF', Fineract has 'USD'",
        "interestRatePerPeriod: wanted 18, Fineract has 20.0",
        "multiDisburseLoan: wanted False, Fineract has True",
        "loanPortfolioAccountId: wanted 12, Fineract has 99",
    ]


def test_mismatch_check_compares_numbers_by_value(cfg: LoanProductConfig) -> None:
    body = build_product_body(cfg, GL_IDS)
    assert product_mismatches(body, existing_product(body)) == []


def test_missing_mapping_in_fineract_is_a_mismatch(cfg: LoanProductConfig) -> None:
    body = build_product_body(cfg, GL_IDS)
    product = existing_product(body)
    del product["accountingMappings"]["writeOffAccount"]
    assert product_mismatches(body, product) == [
        "writeOffAccountId: wanted 51, Fineract has None",
    ]
