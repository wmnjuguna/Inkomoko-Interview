"""/api/accounts routes: create, list, balance."""

from __future__ import annotations

from typing import Any

from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from mock_erp.db import get_engine
from mock_erp.logs import log_field
from mock_erp.schemas.accounts import (
    AccountBalance,
    AccountCreated,
    AccountList,
    AccountListQuery,
    AccountPath,
    CreateAccountRequest,
)
from mock_erp.schemas.common import ErrorResponse
from mock_erp.services import accounts as service

bp = APIBlueprint(
    "accounts", __name__, abp_tags=[Tag(name="accounts")], abp_security=[{"api_key": []}]
)


@bp.post(
    "/api/accounts",
    summary="Create an account (safe to repeat)",
    responses={201: AccountCreated, 200: AccountCreated, 409: ErrorResponse, 422: ErrorResponse},
)
def create_account(body: CreateAccountRequest) -> tuple[dict[str, Any], int]:
    """201 when created, 200 when the same request was already stored."""
    log_field("external_reference", body.external_reference)
    result = service.create_account(get_engine(), body)
    return result.model_dump(mode="json"), 201 if result.status == "CREATED" else 200


@bp.get(
    "/api/accounts",
    summary="List accounts, parents first",
    responses={200: AccountList, 422: ErrorResponse},
)
def list_accounts(query: AccountListQuery) -> tuple[dict[str, Any], int]:
    items = service.list_accounts(get_engine(), query.usage)
    return AccountList(items=items).model_dump(mode="json"), 200


@bp.get(
    "/api/accounts/<code>/balance",
    summary="Account balance (a HEADER adds up its children)",
    responses={200: AccountBalance, 404: ErrorResponse},
)
def account_balance(path: AccountPath) -> tuple[dict[str, Any], int]:
    return service.get_balance(get_engine(), path.code).model_dump(mode="json"), 200
