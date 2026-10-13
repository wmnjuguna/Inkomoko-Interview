"""The loan product: the body built from config, the synced GL ids and Fineract's
GET /loanproducts/template, and ensure_product.

The template fixture is a trimmed copy of the live 1.15 answer (P2.6 probe). The old body
fixture is what the code before P2.6 built from the old loan-product.json with the live GL
ids, so the existing product stays unchanged."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from integration_service.setup.config_files import load_setup_config
from integration_service.setup.product import (
    REQUIRED_CASH_ROLES,
    LoanProductConfig,
    MissingGlAccountsError,
    ProductMismatchError,
    ProductTemplate,
    ProductTemplateError,
    build_product_body,
    ensure_product,
    fetch_product_template,
    parse_product_template,
    product_mismatches,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "fineract_templates"
CONFIG_FILE = Path(__file__).parents[3] / "fineract" / "configuration" / "loan-product.json"

# The Fineract GL ids of the synced ERP accounts, as in the template fixture.
GL_IDS = {
    "GL-1100": 2,
    "GL-1200": 3,
    "GL-1300": 4,
    "GL-2100": 6,
    "GL-4100": 8,
    "GL-4200": 9,
    "GL-4300": 10,
    "GL-4400": 11,
    "GL-5100": 13,
}


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _raw_template() -> dict[str, Any]:
    data: dict[str, Any] = _fixture("loanproducts_template.json")
    return data


@pytest.fixture
def cfg() -> LoanProductConfig:
    return load_setup_config().loan_product


@pytest.fixture
def template() -> ProductTemplate:
    return parse_product_template(_raw_template())


def _raw_config() -> dict[str, Any]:
    return load_setup_config().loan_product.model_dump(by_alias=True)


def _with(**changes: Any) -> LoanProductConfig:
    raw = _raw_config()
    raw.update(changes)
    return LoanProductConfig.model_validate(raw)


# --- the body stays what it was before P2.6 -----------------------------------------------


def test_body_equals_the_body_the_old_code_built(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    assert build_product_body(cfg, GL_IDS, template) == _fixture(
        "loan_product_body_before_p26.json"
    )


def test_body_has_every_field_from_config_template_and_fixed_rules(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    body = build_product_body(cfg, GL_IDS, template)
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
        # Codes in config, ids from the template's options.
        "repaymentFrequencyType": 2,
        "interestRatePerPeriod": 18,
        "minInterestRatePerPeriod": 18,
        "maxInterestRatePerPeriod": 18,
        "interestRateFrequencyType": 3,
        "amortizationType": 1,
        "interestType": 0,
        "interestCalculationPeriodType": 1,
        "transactionProcessingStrategyCode": "mifos-standard-strategy",
        # Not in config: the template's defaults.
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
        "fundSourceAccountId": 2,
        "loanPortfolioAccountId": 3,
        "transfersInSuspenseAccountId": 4,
        "interestOnLoanAccountId": 8,
        "incomeFromFeeAccountId": 9,
        "incomeFromPenaltyAccountId": 10,
        "incomeFromRecoveryAccountId": 11,
        "writeOffAccountId": 13,
        "overpaymentLiabilityAccountId": 6,
    }


def test_body_never_carries_keys_fineract_refuses_for_this_product(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    body = build_product_body(cfg, GL_IDS, template)
    for key in ("chargeOffBehaviour", "supportedInterestRefundTypes", "fixedLength"):
        assert key not in body
    assert "enableAccrualActivityPosting" not in body
    # Older Fineract names that 1.15.0 refuses as unsupported parameters (seen on a real run).
    for key in ("allowPartialPeriodInterestCalcualtion", "canDefineEmiAmount"):
        assert key not in body


# --- codes resolved through the template ---------------------------------------------------


@pytest.mark.parametrize(
    ("config_key", "code", "body_key", "fineract_id"),
    [
        ("repaymentFrequency", "WEEKS", "repaymentFrequencyType", 1),
        ("repaymentFrequency", "DAYS", "repaymentFrequencyType", 0),
        ("interestRateFrequency", "MONTHS", "interestRateFrequencyType", 2),
        ("interestRateFrequency", "WHOLE_TERM", "interestRateFrequencyType", 4),
        ("amortization", "EQUAL_PRINCIPAL", "amortizationType", 0),
        ("interestType", "FLAT", "interestType", 1),
        ("interestCalculationPeriod", "DAILY", "interestCalculationPeriodType", 0),
    ],
)
def test_codes_are_resolved_to_ids_through_the_template(
    template: ProductTemplate, config_key: str, code: str, body_key: str, fineract_id: int
) -> None:
    body = build_product_body(_with(**{config_key: code}), GL_IDS, template)
    assert body[body_key] == fineract_id


def test_ids_follow_the_template_not_the_code(template: ProductTemplate) -> None:
    raw = _raw_template()
    for option in raw["repaymentFrequencyTypeOptions"]:
        if option["code"].endswith(".months"):
            option["id"] = 42
    body = build_product_body(_raw_config_model(), GL_IDS, parse_product_template(raw))
    assert body["repaymentFrequencyType"] == 42


def _raw_config_model() -> LoanProductConfig:
    return LoanProductConfig.model_validate(_raw_config())


def test_unknown_code_is_refused_with_the_allowed_values(template: ProductTemplate) -> None:
    cfg = _with(repaymentFrequency="FORTNIGHTS")
    with pytest.raises(ProductTemplateError) as caught:
        build_product_body(cfg, GL_IDS, template)
    message = str(caught.value)
    assert "repaymentFrequency" in message
    assert "FORTNIGHTS" in message
    assert "DAYS, WEEKS, MONTHS" in message


def test_unknown_amortization_is_refused_with_the_allowed_values(
    template: ProductTemplate,
) -> None:
    with pytest.raises(ProductTemplateError, match="EQUAL_INSTALLMENTS, EQUAL_PRINCIPAL"):
        build_product_body(_with(amortization="BALLOON"), GL_IDS, template)


def test_unknown_strategy_is_refused_with_the_allowed_values(template: ProductTemplate) -> None:
    cfg = _with(transactionProcessingStrategyCode="made-up-strategy")
    with pytest.raises(ProductTemplateError) as caught:
        build_product_body(cfg, GL_IDS, template)
    message = str(caught.value)
    assert "made-up-strategy" in message
    assert "mifos-standard-strategy" in message


def test_currency_fineract_does_not_offer_is_refused(template: ProductTemplate) -> None:
    cfg = _with(currencyCode="KES")
    with pytest.raises(ProductTemplateError, match=r"KES.*RWF, USD"):
        build_product_body(cfg, GL_IDS, template)


def test_gl_account_outside_the_template_options_for_its_role_is_refused(
    template: ProductTemplate,
) -> None:
    raw = _raw_config()
    raw["glAccounts"]["writeOffAccountId"] = "GL-4100"  # an income account, not an expense
    cfg = LoanProductConfig.model_validate(raw)
    with pytest.raises(ProductTemplateError) as caught:
        build_product_body(cfg, GL_IDS, template)
    message = str(caught.value)
    assert "writeOffAccountId" in message
    assert "GL-4100" in message
    assert "expenseAccountOptions" in message


def test_every_role_is_checked_against_its_own_list(template: ProductTemplate) -> None:
    raw = _raw_config()
    raw["glAccounts"]["fundSourceAccountId"] = "GL-2100"  # a liability, not an asset
    raw["glAccounts"]["overpaymentLiabilityAccountId"] = "GL-1100"  # an asset
    cfg = LoanProductConfig.model_validate(raw)
    with pytest.raises(ProductTemplateError) as caught:
        build_product_body(cfg, GL_IDS, template)
    assert "fundSourceAccountId" in str(caught.value)
    assert "overpaymentLiabilityAccountId" in str(caught.value)


# --- template defaults and the template's shape --------------------------------------------


def test_days_and_start_date_come_from_the_template_defaults() -> None:
    raw = _raw_template()
    raw["daysInYearType"] = next(o for o in raw["daysInYearTypeOptions"] if o["id"] == 365)
    raw["daysInMonthType"] = next(o for o in raw["daysInMonthTypeOptions"] if o["id"] == 30)
    raw["repaymentStartDateType"] = next(
        o for o in raw["repaymentStartDateTypeOptions"] if o["id"] == 2
    )
    body = build_product_body(_raw_config_model(), GL_IDS, parse_product_template(raw))
    assert body["daysInYearType"] == 365
    assert body["daysInMonthType"] == 30
    assert body["repaymentStartDateType"] == 2


def test_template_without_an_options_list_is_refused() -> None:
    raw = _raw_template()
    del raw["amortizationTypeOptions"]
    with pytest.raises(ProductTemplateError, match="amortizationTypeOptions"):
        parse_product_template(raw)


def test_template_without_a_default_is_refused() -> None:
    raw = _raw_template()
    del raw["daysInYearType"]
    with pytest.raises(ProductTemplateError, match="daysInYearType"):
        parse_product_template(raw)


def test_template_without_cash_accounting_is_refused() -> None:
    raw = _raw_template()
    raw["accountingRuleOptions"] = [
        o for o in raw["accountingRuleOptions"] if o["code"] != "accountingRuleType.cash"
    ]
    with pytest.raises(ProductTemplateError, match="CASH"):
        build_product_body(_raw_config_model(), GL_IDS, parse_product_template(raw))


def test_template_without_cumulative_schedule_is_refused() -> None:
    raw = _raw_template()
    raw["loanScheduleTypeOptions"] = [
        o for o in raw["loanScheduleTypeOptions"] if o["code"] != "CUMULATIVE"
    ]
    with pytest.raises(ProductTemplateError, match="CUMULATIVE"):
        build_product_body(_raw_config_model(), GL_IDS, parse_product_template(raw))


def test_template_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(ProductTemplateError):
        parse_product_template([])


def test_fetch_reads_the_loan_product_template() -> None:
    class Reader:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def get(self, path: str, params: Any = None, correlation_id: Any = None) -> Any:
            self.paths.append(path)
            return copy.deepcopy(_raw_template())

    reader = Reader()
    assert fetch_product_template(reader) == parse_product_template(_raw_template())
    assert reader.paths == ["/loanproducts/template"]


# --- the config file ------------------------------------------------------------------------


def test_config_file_has_no_fineract_numeric_ids() -> None:
    raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    enum_keys = {k for k in raw if k.endswith("Type")}
    assert enum_keys == {"interestType"}
    assert isinstance(raw["interestType"], str)
    for key in ("daysInYearType", "daysInMonthType", "repaymentStartDateType"):
        assert key not in raw
    assert all(isinstance(v, str) for v in raw["glAccounts"].values())


def test_config_with_old_numeric_enum_keys_is_refused() -> None:
    raw = _raw_config()
    raw["repaymentFrequencyType"] = 2
    with pytest.raises(ValidationError, match="repaymentFrequencyType"):
        LoanProductConfig.model_validate(raw)


def test_every_required_cash_role_is_mapped_in_the_config(cfg: LoanProductConfig) -> None:
    assert set(cfg.gl_accounts) == set(REQUIRED_CASH_ROLES)
    assert len(REQUIRED_CASH_ROLES) == 9


def test_missing_synced_account_is_refused_with_the_roles_named(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    ids = {ref: gl_id for ref, gl_id in GL_IDS.items() if ref not in ("GL-1200", "GL-5100")}
    with pytest.raises(MissingGlAccountsError) as caught:
        build_product_body(cfg, ids, template)
    assert caught.value.missing == {
        "loanPortfolioAccountId": "GL-1200",
        "writeOffAccountId": "GL-5100",
    }
    assert "GL-1200" in str(caught.value)


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
        ("repaymentFrequency", ""),  # a code is needed
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
        "daysInYearType": {"id": body["daysInYearType"]},
        "daysInMonthType": {"id": body["daysInMonthType"]},
        "repaymentStartDateType": {"id": body["repaymentStartDateType"]},
        "loanScheduleType": {"id": 0, "code": body["loanScheduleType"], "value": "Cumulative"},
        "multiDisburseLoan": False,
        "accountingRule": {"id": 2, "code": "accountingRuleType.cash", "value": "CASH BASED"},
        "accountingMappings": mappings,
    }


def test_ensure_product_creates_it_when_missing(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    body = build_product_body(cfg, GL_IDS, template)
    fake = FakeFineract([{"id": 3, "name": "Other product"}])
    assert ensure_product(fake, body) == 77
    assert fake.writes == [("POST", "/loanproducts", body)]


def test_ensure_product_returns_the_existing_id_and_writes_nothing(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    body = build_product_body(cfg, GL_IDS, template)
    fake = FakeFineract([existing_product(body)])
    assert ensure_product(fake, body) == 5
    assert fake.writes == []


def test_existing_product_with_other_settings_is_reported_not_changed(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    body = build_product_body(cfg, GL_IDS, template)
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
        "loanPortfolioAccountId: wanted 3, Fineract has 99",
    ]


def test_mismatch_check_compares_numbers_by_value(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    body = build_product_body(cfg, GL_IDS, template)
    assert product_mismatches(body, existing_product(body)) == []


def test_missing_mapping_in_fineract_is_a_mismatch(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    body = build_product_body(cfg, GL_IDS, template)
    product = existing_product(body)
    del product["accountingMappings"]["writeOffAccount"]
    assert product_mismatches(body, product) == [
        "writeOffAccountId: wanted 13, Fineract has None",
    ]


def test_template_defaults_and_schedule_type_are_compared_on_a_rerun(
    cfg: LoanProductConfig, template: ProductTemplate
) -> None:
    # These four are not in the config: the defaults come from the template and the schedule
    # type is fixed in code. A product that differs on them must stop setup, not pass as "exists".
    body = build_product_body(cfg, GL_IDS, template)
    product = existing_product(body)
    product["daysInYearType"] = {"id": 360}
    product["daysInMonthType"] = {"id": 30}
    product["repaymentStartDateType"] = {"id": 2}
    product["loanScheduleType"] = {"id": 1, "code": "PROGRESSIVE", "value": "Progressive"}
    assert product_mismatches(body, product) == [
        "daysInYearType: wanted 1, Fineract has 360",
        "daysInMonthType: wanted 1, Fineract has 30",
        "repaymentStartDateType: wanted 1, Fineract has 2",
        "loanScheduleType: wanted 'CUMULATIVE', Fineract has 'PROGRESSIVE'",
    ]
