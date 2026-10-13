"""Account rules that need no database: validation, hierarchy, hashing, balance math."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mock_erp.errors import ErpError, ErrorCode
from mock_erp.hashing import request_hash
from mock_erp.schemas.accounts import CreateAccountRequest
from mock_erp.seed import SEED_ACCOUNTS
from mock_erp.services.accounts import (
    ParentInfo,
    account_hash,
    balance_for,
    check_parent,
    hash_fields,
    normal_side,
)


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": "1400",
        "external_reference": "GL-1400",
        "name": "Mobile Money",
        "type": "ASSET",
        "usage": "DETAIL",
        "parent_code": "1000",
    }
    body.update(changes)
    return body


def test_valid_request_defaults_currency_to_rwf_and_strips_name() -> None:
    req = CreateAccountRequest.model_validate(_body(name="  Mobile Money  "))
    assert req.currency == "RWF"
    assert req.name == "Mobile Money"


def test_parent_code_is_optional() -> None:
    req = CreateAccountRequest.model_validate(_body(usage="HEADER", parent_code=None))
    assert req.parent_code is None
    body = _body()
    del body["parent_code"]
    assert CreateAccountRequest.model_validate(body).parent_code is None


@pytest.mark.parametrize("code", ["123", "12345678901", "12a4", "", " 1400"])
def test_bad_code_is_refused(code: str) -> None:
    with pytest.raises(ValidationError):
        CreateAccountRequest.model_validate(_body(code=code))


@pytest.mark.parametrize("currency", ["USD", "rwf", ""])
def test_only_rwf_is_accepted(currency: str) -> None:
    with pytest.raises(ValidationError):
        CreateAccountRequest.model_validate(_body(currency=currency))


@pytest.mark.parametrize("ref", ["", "-GL", "GL 1400", "x" * 65, "GL/1400"])
def test_bad_external_reference_is_refused(ref: str) -> None:
    with pytest.raises(ValidationError):
        CreateAccountRequest.model_validate(_body(external_reference=ref))


def test_good_external_reference_shapes() -> None:
    for ref in ["GL-1400", "a", "acct:1400_v2.x", "x" * 64]:
        assert CreateAccountRequest.model_validate(_body(external_reference=ref))


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_bad_name_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        CreateAccountRequest.model_validate(_body(name=name))


@pytest.mark.parametrize("field,value", [("type", "ASSETS"), ("usage", "detail")])
def test_bad_type_or_usage_is_refused(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        CreateAccountRequest.model_validate(_body(**{field: value}))


# Hierarchy


def _parent_error(usage: str, type_: str, code: str | None, parent: ParentInfo | None) -> ErpError:
    with pytest.raises(ErpError) as caught:
        check_parent(usage, type_, code, parent)
    assert caught.value.code == ErrorCode.PARENT_INVALID
    assert caught.value.status == 422
    return caught.value


def test_detail_under_header_of_same_type_is_fine() -> None:
    check_parent("DETAIL", "ASSET", "1000", ParentInfo(usage="HEADER", type="ASSET"))


def test_header_may_stand_alone_or_sit_under_a_header() -> None:
    check_parent("HEADER", "INCOME", None, None)
    check_parent("HEADER", "INCOME", "4000", ParentInfo(usage="HEADER", type="INCOME"))


def test_missing_parent_is_refused() -> None:
    err = _parent_error("DETAIL", "ASSET", "9999", None)
    assert err.details == {"parent_code": "9999"}


def test_detail_parent_is_refused() -> None:
    _parent_error("DETAIL", "ASSET", "1100", ParentInfo(usage="DETAIL", type="ASSET"))
    _parent_error("HEADER", "ASSET", "1100", ParentInfo(usage="DETAIL", type="ASSET"))


def test_parent_of_another_type_is_refused() -> None:
    _parent_error("DETAIL", "EXPENSE", "1000", ParentInfo(usage="HEADER", type="ASSET"))


def test_detail_without_parent_is_refused() -> None:
    _parent_error("DETAIL", "ASSET", None, None)


# Balance math


@pytest.mark.parametrize(
    "type_,side",
    [
        ("ASSET", "DEBIT"),
        ("EXPENSE", "DEBIT"),
        ("LIABILITY", "CREDIT"),
        ("EQUITY", "CREDIT"),
        ("INCOME", "CREDIT"),
    ],
)
def test_normal_side_by_type(type_: str, side: str) -> None:
    assert normal_side(type_) == side


def test_balance_is_normal_side_minus_other_side() -> None:
    assert balance_for("ASSET", debit_total=500, credit_total=200) == 300
    assert balance_for("INCOME", debit_total=50, credit_total=200) == 150
    assert balance_for("LIABILITY", debit_total=300, credit_total=100) == -200


# Hash


def test_hash_uses_exactly_the_contract_fields() -> None:
    req = CreateAccountRequest.model_validate(_body())
    assert set(hash_fields(req)) == {
        "code",
        "external_reference",
        "name",
        "type",
        "usage",
        "parent_code",
        "currency",
    }


def test_hash_matches_the_seed_hash() -> None:
    seed = SEED_ACCOUNTS[1]
    req = CreateAccountRequest.model_validate(seed)
    assert account_hash(req) == request_hash({**seed, "currency": "RWF"})


def test_name_whitespace_does_not_change_the_hash() -> None:
    a = CreateAccountRequest.model_validate(_body(name="Bank"))
    b = CreateAccountRequest.model_validate(_body(name="  Bank "))
    assert account_hash(a) == account_hash(b)
