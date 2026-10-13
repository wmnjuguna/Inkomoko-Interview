"""Form endpoints: what to send at each step, with defaults, ranges and options taken from
Fineract's own templates (services/templates.py). Read only. Answers carry our identifiers
only, never Fineract ids."""

from __future__ import annotations

from typing import Any

from flask import g
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from integration_service.clients.errors import FineractError, FineractNotFound
from integration_service.config import get_settings
from integration_service.db import get_engine
from integration_service.errors import ApiError, ErrorCode
from integration_service.logs import log_field
from integration_service.schemas.common import API_ERRORS, UPSTREAM_ERRORS, ErrorResponse
from integration_service.schemas.loans import LoanPath
from integration_service.schemas.templates import (
    ClientFormResponse,
    LoanApplicationFormResponse,
    LoanFormQuery,
    StepFormResponse,
    StepQuery,
)
from integration_service.services import templates
from integration_service.services.clients import our_client
from integration_service.services.loans import (
    fineract_from_settings,
    fineract_read_error,
    get_fineract_loan,
    kigali_today,
    loan_status_code,
    our_loan,
)

bp = APIBlueprint(
    "templates",
    __name__,
    abp_tags=[Tag(name="forms")],
    abp_security=[{"api_key": []}],
    abp_responses=API_ERRORS,
)


def _correlation_id() -> str | None:
    value = g.get("correlation_id")
    return value if isinstance(value, str) else None


def _loan_missing(loan_reference: str) -> ApiError:
    return ApiError(ErrorCode.LOAN_NOT_FOUND, f"No loan {loan_reference}.", 404)


@bp.get(
    "/api/clients/template",
    summary="Client form",
    description="The fields of POST /api/clients with their rules, and the office and "
    "activation date Fineract uses for a new client (from Fineract's client template).",
    responses={200: ClientFormResponse, **UPSTREAM_ERRORS},
)
def get_client_form() -> tuple[dict[str, Any], int]:
    fineract = fineract_from_settings(get_settings())
    try:
        form = templates.fetch_client_form(fineract, _correlation_id())
    except FineractError as exc:
        raise fineract_read_error(exc) from exc
    finally:
        fineract.close()
    return templates.client_form_view(form), 200


@bp.get(
    "/api/loans/template",
    summary="Loan application form",
    description="The fields of POST /api/loans with the SME Asset Finance product's defaults, "
    "ranges and terms, from Fineract's loan template. Add ?client_uid= for that client's "
    "defaults. For the later steps of one loan, see GET /api/loans/{loan_reference}/template.",
    responses={200: LoanApplicationFormResponse, 404: ErrorResponse, **UPSTREAM_ERRORS},
)
def get_loan_application_form(query: LoanFormQuery) -> tuple[dict[str, Any], int]:
    fineract_client_id = None
    if query.client_uid is not None:
        log_field("client_uid", query.client_uid)
        client = our_client(get_engine(), query.client_uid)
        if client is None or client.fineract_client_id is None:
            raise ApiError(
                ErrorCode.CLIENT_NOT_FOUND,
                f"No client {query.client_uid}. Create it with POST /api/clients first.",
                404,
            )
        fineract_client_id = client.fineract_client_id
    fineract = fineract_from_settings(get_settings())
    try:
        form = templates.fetch_loan_application_form(
            fineract, fineract_client_id, correlation_id=_correlation_id()
        )
    except FineractError as exc:
        raise fineract_read_error(exc) from exc
    finally:
        fineract.close()
    return templates.loan_application_form_view(form, query.client_uid, kigali_today()), 200


@bp.get(
    "/api/loans/<loan_reference>/template",
    summary="Form for the next step of a loan",
    description="The fields of the approve, disburse or repayments call for this loan, with "
    "Fineract's defaults for it now: the approved amount, the expected disbursement amount and "
    "date, the next installment, or the payoff amount (step=payoff, sent as a repayment). "
    "available says whether the loan's status allows the step now.",
    responses={200: StepFormResponse, 404: ErrorResponse, **UPSTREAM_ERRORS},
)
def get_step_form(path: LoanPath, query: StepQuery) -> tuple[dict[str, Any], int]:
    reference = path.loan_reference
    log_field("loan_reference", reference)
    loan = our_loan(get_engine(), reference)
    if loan is None:
        raise _loan_missing(reference)
    log_field("client_uid", loan.client_uid)
    fineract = fineract_from_settings(get_settings())
    try:
        data = get_fineract_loan(fineract, reference, _correlation_id())
        if data is None:
            raise _loan_missing(reference)
        form = templates.fetch_step_form(fineract, reference, query.step, _correlation_id())
    except FineractNotFound as exc:
        raise _loan_missing(reference) from exc
    except FineractError as exc:
        raise fineract_read_error(exc) from exc
    finally:
        fineract.close()
    status = loan_status_code((data.get("status") or {}).get("code"))
    log_field("status", status)
    body = templates.step_form_view(
        query.step, form, reference, loan.client_uid, status, kigali_today()
    )
    return body, 200
