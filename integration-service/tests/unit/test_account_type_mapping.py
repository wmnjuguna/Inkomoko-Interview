"""The one place ERP account types become Fineract GL numbers, and the plugin body."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from integration_service.clients.erp import ErpAccount
from integration_service.mapping.account_types import (
    FINERACT_GL_TYPES,
    FINERACT_GL_USAGES,
    MissingParentReferenceError,
    UnknownAccountTypeError,
    UnknownAccountUsageError,
    build_fineract_gl_body,
    fineract_gl_type,
    fineract_gl_usage,
)


def account(**changes: Any) -> ErpAccount:
    data: dict[str, Any] = {
        "code": "1200",
        "external_reference": "GL-1200",
        "name": "Loan Receivable",
        "type": "ASSET",
        "usage": "DETAIL",
        "parent_code": "1000",
        "currency": "RWF",
        "created_at": datetime(2026, 10, 13, tzinfo=UTC),
    }
    data.update(changes)
    return ErpAccount.model_validate(data)


@pytest.mark.parametrize(
    ("erp_type", "number"),
    [("ASSET", 1), ("LIABILITY", 2), ("EQUITY", 3), ("INCOME", 4), ("EXPENSE", 5)],
)
def test_every_erp_type_maps_to_its_fineract_number(erp_type: str, number: int) -> None:
    assert fineract_gl_type(erp_type) == number


def test_the_type_table_covers_exactly_the_five_types() -> None:
    assert FINERACT_GL_TYPES == {
        "ASSET": 1,
        "LIABILITY": 2,
        "EQUITY": 3,
        "INCOME": 4,
        "EXPENSE": 5,
    }


@pytest.mark.parametrize(("usage", "number"), [("DETAIL", 1), ("HEADER", 2)])
def test_usage_maps_to_fineract_numbers(usage: str, number: int) -> None:
    # Fineract's GLAccountUsage: 1 DETAIL, 2 HEADER (API.md, plugin section).
    assert fineract_gl_usage(usage) == number


def test_the_usage_table_covers_exactly_header_and_detail() -> None:
    assert FINERACT_GL_USAGES == {"DETAIL": 1, "HEADER": 2}


@pytest.mark.parametrize("bad", ["asset", "REVENUE", "", "1"])
def test_unknown_type_is_a_clear_error(bad: str) -> None:
    with pytest.raises(UnknownAccountTypeError, match="ERP account type"):
        fineract_gl_type(bad)


@pytest.mark.parametrize("bad", ["detail", "SUMMARY", ""])
def test_unknown_usage_is_a_clear_error(bad: str) -> None:
    with pytest.raises(UnknownAccountUsageError, match="ERP account usage"):
        fineract_gl_usage(bad)


def test_detail_child_body() -> None:
    body = build_fineract_gl_body(account(), parent_reference="GL-1000")
    assert body == {
        "name": "Loan Receivable",
        "glCode": "1200",
        "type": 1,
        "usage": 1,
        "manualEntriesAllowed": True,
        "description": "Synced from ERP account GL-1200",
        "parentAccount": "GL-1000",
    }


def test_header_without_parent_has_no_parent_and_no_manual_entries() -> None:
    header = account(
        code="1000", external_reference="GL-1000", name="Assets", usage="HEADER", parent_code=None
    )
    body = build_fineract_gl_body(header)
    assert body == {
        "name": "Assets",
        "glCode": "1000",
        "type": 1,
        "usage": 2,
        "manualEntriesAllowed": False,
        "description": "Synced from ERP account GL-1000",
    }
    assert "parentAccount" not in body


@pytest.mark.parametrize(
    ("erp_type", "number"),
    [("ASSET", 1), ("LIABILITY", 2), ("EQUITY", 3), ("INCOME", 4), ("EXPENSE", 5)],
)
def test_body_carries_the_mapped_type(erp_type: str, number: int) -> None:
    assert build_fineract_gl_body(account(type=erp_type), "GL-1000")["type"] == number


def test_a_child_needs_its_parents_reference() -> None:
    with pytest.raises(MissingParentReferenceError, match=r"GL-1200.*1000"):
        build_fineract_gl_body(account(parent_code="1000"), parent_reference=None)


def test_a_top_level_account_ignores_no_parent() -> None:
    top = account(parent_code=None, usage="DETAIL")
    assert "parentAccount" not in build_fineract_gl_body(top, parent_reference=None)


def test_the_body_does_not_depend_on_currency_or_created_at() -> None:
    a = build_fineract_gl_body(account(), "GL-1000")
    b = build_fineract_gl_body(
        account(currency="USD", created_at=datetime(2030, 1, 1, tzinfo=UTC)), "GL-1000"
    )
    assert a == b
