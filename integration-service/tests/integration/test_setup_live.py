"""Setup inputs and readers against the running Fineract. READ ONLY.

Fineract is shared, so these tests never create an office, role, user or product. Every
call goes through a guard that fails the test on any POST or PUT. The real setup run is
done once on a clean stack, outside the tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from integration_service.clients.fineract import FineractClient
from integration_service.setup.config_files import SetupConfig, load_setup_config
from integration_service.setup.product import (
    REQUIRED_CASH_ROLES,
    build_product_body,
    parse_product_template,
)
from integration_service.setup.steps import (
    check_permission_codes,
    ensure_currency,
    find_office,
    find_office_by_external_id,
    find_role,
    find_user,
    fineract_date,
    option_id,
    role_permissions,
)


class ReadOnly:
    """Passes GETs to Fineract and fails on any write."""

    def __init__(self, client: FineractClient) -> None:
        self.client = client

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        return self.client.get(path, params=params, correlation_id=correlation_id)

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        raise AssertionError(f"live setup test tried POST {path}")

    def put(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        raise AssertionError(f"live setup test tried PUT {path}")


@pytest.fixture(scope="module")
def fineract() -> Iterator[ReadOnly]:
    client = FineractClient(
        base_url=os.environ.get(
            "FINERACT_BASE_URL", "http://localhost:8080/fineract-provider/api/v1"
        ),
        tenant=os.environ.get("FINERACT_TENANT", "default"),
        username=os.environ.get("FINERACT_USERNAME", "mifos"),
        password=os.environ.get("FINERACT_PASSWORD", "password"),
        timeout_seconds=30,
    )
    yield ReadOnly(client)
    client.close()


@pytest.fixture(scope="module")
def cfg() -> SetupConfig:
    return load_setup_config()


def test_every_configured_permission_code_exists(fineract: ReadOnly, cfg: SetupConfig) -> None:
    codes = {p["code"] for p in fineract.get("/permissions")}
    assert set(cfg.role.permissions) <= codes
    # The plugin's own permission is there too, so the role can sync GL accounts.
    assert "SYNC_GLACCOUNT" in codes


def test_role_permissions_reads_the_live_shape(fineract: ReadOnly, cfg: SetupConfig) -> None:
    known, held = role_permissions(fineract, 1)
    assert set(cfg.role.permissions) <= known
    assert "ALL_FUNCTIONS" in held
    assert held <= known


def test_finders_read_the_live_shapes(fineract: ReadOnly) -> None:
    head = find_office(fineract, "Head Office")
    assert head is not None
    assert head["id"] == 1
    assert head.get("parentId") is None
    role = find_role(fineract, "Super user")
    assert role is not None
    assert role["id"] == 1
    admin = find_user(fineract, "mifos")
    assert admin is not None
    assert admin["officeId"] == 1
    assert 1 in {r["id"] for r in admin["selectedRoles"]}
    assert find_office(fineract, "No such office here") is None
    assert find_user(fineract, "no-such-user-here") is None


def test_office_template_offers_the_configured_parent(fineract: ReadOnly, cfg: SetupConfig) -> None:
    template = fineract.get("/offices/template")
    parent_id = option_id(template["allowedParents"], cfg.office.parent, "Parent office")
    parent = fineract.get(f"/offices/{parent_id}")
    assert fineract_date(parent["openingDate"]) <= cfg.office.opening_date


def test_office_lookup_by_external_id_reads_the_live_shapes(fineract: ReadOnly) -> None:
    head = find_office_by_external_id(fineract, "1")  # Head Office's externalId on 1.15
    assert head is not None
    assert head["name"] == "Head Office"
    assert find_office_by_external_id(fineract, "NO-SUCH-OFFICE-HERE") is None


def test_users_template_offers_offices_and_roles_by_name(fineract: ReadOnly) -> None:
    template = fineract.get("/users/template")
    assert option_id(template["allowedOffices"], "Head Office", "Office") == 1
    assert option_id(template["availableRoles"], "Super user", "Role") == 1


def test_configured_permission_codes_pass_the_check(fineract: ReadOnly, cfg: SetupConfig) -> None:
    check_permission_codes(fineract, cfg.role.permissions)


def test_currency_already_selected_needs_no_write(fineract: ReadOnly) -> None:
    selected = fineract.get("/currencies")["selectedCurrencyOptions"]
    code = selected[0]["code"]
    assert ensure_currency(fineract, code) is False  # the guard fails on any PUT


def test_configured_currency_exists_with_no_decimals(fineract: ReadOnly, cfg: SetupConfig) -> None:
    currencies = fineract.get("/currencies")
    every = currencies["currencyOptions"] + currencies["selectedCurrencyOptions"]
    found = [c for c in every if c["code"] == cfg.currency.code]
    assert found
    assert found[0]["decimalPlaces"] == cfg.loan_product.digits_after_decimal == 0


def _ids(options: list[dict[str, Any]]) -> set[Any]:
    return {o["id"] for o in options}


def test_product_body_uses_only_values_fineract_offers(
    fineract: ReadOnly, cfg: SetupConfig
) -> None:
    template = fineract.get("/loanproducts/template")
    by_code = {
        account["glCode"]: account["id"]
        for accounts in template["accountingMappingOptions"].values()
        for account in accounts
    }
    gl_ids = {
        ref: by_code[ref.removeprefix("GL-")] for ref in cfg.loan_product.gl_accounts.values()
    }
    body = build_product_body(cfg.loan_product, gl_ids, parse_product_template(template))
    for key in (
        "repaymentFrequencyType",
        "interestRateFrequencyType",
        "amortizationType",
        "interestType",
        "interestCalculationPeriodType",
        "daysInYearType",
        "daysInMonthType",
        "repaymentStartDateType",
        "accountingRule",
    ):
        assert body[key] in _ids(template[f"{key}Options"]), key
    assert body["transactionProcessingStrategyCode"] in {
        o["code"] for o in template["transactionProcessingStrategyOptions"]
    }
    assert body["loanScheduleType"] in {o["code"] for o in template["loanScheduleTypeOptions"]}
    cash = next(o for o in template["accountingRuleOptions"] if o["id"] == body["accountingRule"])
    assert cash["code"] == "accountingRuleType.cash"
    assert all(role in body for role in REQUIRED_CASH_ROLES)
