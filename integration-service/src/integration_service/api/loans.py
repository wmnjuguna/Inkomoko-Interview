"""/api/loans: application, approval, the loan as Fineract reports it, disbursement and
repayments (Parts D and E). The work is in services/loans.py and services/money.py.
Answers carry our identifiers only, never Fineract ids.

To add a route: call the service inside `with fineract() as f:`
and return `respond(answer)`. Both log the section 9 fields for the request line."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from flask import current_app, g
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from integration_service.config import get_settings
from integration_service.db import get_engine
from integration_service.logs import log_field
from integration_service.schemas.common import API_ERRORS, UPSTREAM_ERRORS, ErrorResponse
from integration_service.schemas.loans import (
    AcceptedResponse,
    ApprovalRequest,
    ApprovalResponse,
    LoanApplicationRequest,
    LoanApplicationResponse,
    LoanPath,
    LoanResponse,
)
from integration_service.schemas.money import (
    DisbursementRequest,
    DisbursementResponse,
    RepaymentRequest,
    RepaymentResponse,
)
from integration_service.services.loans import (
    Answer,
    FineractApi,
    apply_for_loan,
    approve_loan,
    fineract_from_settings,
    loan_details,
)
from integration_service.services.money import Accounts, disburse_loan, record_repayment
from integration_service.services.recovery import gl_role_accounts_from

ACCOUNTS_KEY = "integration_service.gl_role_accounts"

bp = APIBlueprint(
    "loans",
    __name__,
    abp_tags=[Tag(name="loans")],
    abp_security=[{"api_key": []}],
    abp_responses=API_ERRORS,
)


@contextmanager
def fineract() -> Iterator[FineractApi]:
    client = fineract_from_settings(get_settings())
    try:
        yield client
    finally:
        client.close()


def correlation_id() -> str | None:
    value = g.get("correlation_id")
    return value if isinstance(value, str) else None


def gl_accounts() -> Accounts:
    """The product's GL role accounts (loan-product.json), read once by the app factory."""
    accounts: Accounts | None = current_app.extensions.get(ACCOUNTS_KEY)
    return accounts or gl_role_accounts_from(get_settings().gl_roles_config_path)


def respond(answer: Answer) -> tuple[dict[str, Any], int]:
    """Send a service answer, and put its identifiers and status on the request log line."""
    for name in ("client_uid", "loan_reference", "status"):
        if answer.body.get(name) is not None:
            log_field(name, answer.body[name])
    return answer.body, answer.status


@bp.post(
    "/api/loans",
    summary="Apply for a loan",
    description="Submits a loan application in Fineract for an existing client, on the SME "
    "Asset Finance product. The principal range, term, rate and schedule come from Fineract's "
    "loan template for the product (see GET /api/loans/template); a principal outside the "
    "range is 422 VALIDATION_ERROR with details.problems. Returns the new loan_reference. "
    "201 the first time; the same external_reference and data again gives 200 with the same "
    "body (the same loan_reference).",
    responses={
        201: LoanApplicationResponse,
        200: LoanApplicationResponse,
        202: AcceptedResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        **UPSTREAM_ERRORS,
    },
)
def post_loan(body: LoanApplicationRequest) -> tuple[dict[str, Any], int]:
    log_field("external_reference", body.external_reference)
    log_field("client_uid", body.client_uid)
    with fineract() as f:
        answer = apply_for_loan(get_engine(), f, body, g.partner, correlation_id())
    return respond(answer)


@bp.post(
    "/api/loans/<loan_reference>/approve",
    summary="Approve a loan",
    description="Approves the loan in Fineract for the amount its approval template proposes "
    "(the applied principal). approved_on defaults to Fineract's proposed date (today). A loan "
    "is approved once: another external_reference for the same loan gives 409 "
    "LOAN_ALREADY_APPROVED.",
    responses={
        200: ApprovalResponse,
        202: AcceptedResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        **UPSTREAM_ERRORS,
    },
)
def post_approval(path: LoanPath, body: ApprovalRequest) -> tuple[dict[str, Any], int]:
    log_field("external_reference", body.external_reference)
    log_field("loan_reference", path.loan_reference)
    with fineract() as f:
        answer = approve_loan(
            get_engine(), f, path.loan_reference, body, g.partner, correlation_id()
        )
    return respond(answer)


@bp.get(
    "/api/loans/<loan_reference>",
    summary="Loan status and balances",
    description="The loan's status and balances as Fineract reports them right now, in "
    "whole RWF. balances is null until the loan is disbursed.",
    responses={200: LoanResponse, 404: ErrorResponse, **UPSTREAM_ERRORS},
)
def get_loan(path: LoanPath) -> tuple[dict[str, Any], int]:
    log_field("loan_reference", path.loan_reference)
    with fineract() as f:
        body = loan_details(get_engine(), f, path.loan_reference, correlation_id())
    log_field("client_uid", body["client_uid"])
    log_field("status", body["status"]["code"])
    return body, 200


@bp.post(
    "/api/loans/<loan_reference>/disburse",
    summary="Pay out a loan",
    description="Pays out the loan's full approved principal in Fineract, once. The answer "
    "comes from Fineract's own transaction; its journal entry goes to the ERP through the "
    "outbox (erp_status). Another external_reference for the same loan gives 409 "
    "LOAN_ALREADY_DISBURSED, also while the first one is still running.",
    responses={
        200: DisbursementResponse,
        202: AcceptedResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        **UPSTREAM_ERRORS,
    },
)
def post_disbursement(path: LoanPath, body: DisbursementRequest) -> tuple[dict[str, Any], int]:
    log_field("external_reference", body.external_reference)
    log_field("loan_reference", path.loan_reference)
    with fineract() as f:
        answer = disburse_loan(
            get_engine(),
            f,
            path.loan_reference,
            body,
            g.partner,
            correlation_id(),
            gl_accounts(),
        )
    return respond(answer)


@bp.post(
    "/api/loans/<loan_reference>/repayments",
    summary="Record a repayment",
    description="Records a repayment in Fineract while the loan is ACTIVE. The split into "
    "principal, interest and overpayment is Fineract's own; its journal entry goes to the "
    "ERP through the outbox (erp_status). A loan takes many repayments, one per "
    "external_reference.",
    responses={
        200: RepaymentResponse,
        202: AcceptedResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        **UPSTREAM_ERRORS,
    },
)
def post_repayment(path: LoanPath, body: RepaymentRequest) -> tuple[dict[str, Any], int]:
    log_field("external_reference", body.external_reference)
    log_field("loan_reference", path.loan_reference)
    with fineract() as f:
        answer = record_repayment(
            get_engine(),
            f,
            path.loan_reference,
            body,
            g.partner,
            correlation_id(),
            gl_accounts(),
        )
    return respond(answer)
