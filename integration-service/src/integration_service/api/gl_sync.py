"""GL sync: ERP chart of accounts to Fineract.

POST /api/accounts/sync runs the sync for the whole chart and answers with the state of
every account. POST /api/accounts/{erp_reference}/sync does the same for one account, keyed
by its ERP external_reference. GET only reads the state. While another sync runs, both POSTs
answer 202 with the current state and change nothing.

Why the chart-wide call needs no external_reference in the body: the state-changing work is
per account, and each one is keyed by its own ERP external_reference (one gl_sync row, one
plugin mapping, Idempotency-Key GLS:<reference>:...). The data comes from the ERP, not the
caller, so there is no caller payload that could differ for the same reference.
These routes use the partner key like every /api route. In production they would sit on an
internal or admin route: syncing the chart is an operator task, not a partner one."""

from __future__ import annotations

from typing import Any

from flask import g
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from integration_service.clients.erp import ErpClient
from integration_service.clients.errors import ErpError, ErpTimeout, ErpUnavailable
from integration_service.clients.fineract import FineractClient
from integration_service.config import get_settings
from integration_service.db import get_engine
from integration_service.errors import ApiError, ErrorCode
from integration_service.logs import log_field
from integration_service.schemas.common import API_ERRORS, UPSTREAM_ERRORS, ErrorResponse
from integration_service.schemas.gl_sync import (
    AccountSyncPath,
    AccountSyncResponse,
    SyncReportResponse,
)
from integration_service.services.gl_sync import (
    SyncInProgressError,
    get_report,
    sync_all,
    sync_one,
)

bp = APIBlueprint(
    "gl_sync",
    __name__,
    abp_tags=[Tag(name="gl-sync")],
    abp_security=[{"api_key": []}],
    abp_responses=API_ERRORS,
)


@bp.post(
    "/api/accounts/sync",
    summary="Sync the ERP chart of accounts to Fineract",
    description="Creates in Fineract every ERP account not synced yet, parents first. Safe to "
    "call again: synced accounts are not sent again, and pending or failed ones are retried. "
    "If a sync is already running, answers 202 with the current state and in_progress true.",
    responses={200: SyncReportResponse, 202: SyncReportResponse, **UPSTREAM_ERRORS},
)
def run_sync() -> tuple[dict[str, Any], int]:
    erp, fineract = _clients()
    try:
        report = sync_all(get_engine(), erp, fineract, correlation_id=g.get("correlation_id"))
    except ErpError as exc:
        raise _erp_api_error(exc) from exc
    finally:
        erp.close()
        fineract.close()
    status = 202 if report.in_progress else 200
    return SyncReportResponse.from_report(report).model_dump(), status


@bp.post(
    "/api/accounts/<erp_reference>/sync",
    summary="Sync one ERP account to Fineract",
    description="Creates the account in Fineract if it is not synced yet. Its parent must be "
    "SYNCED already. Safe to call again. If a sync is already running, answers 202 with the "
    "account's current state (null if never recorded) and in_progress true.",
    responses={
        200: AccountSyncResponse,
        202: AccountSyncResponse,
        404: ErrorResponse,
        **UPSTREAM_ERRORS,
    },
)
def run_sync_one(path: AccountSyncPath) -> tuple[dict[str, Any], int]:
    reference = path.erp_reference
    log_field("external_reference", reference)
    erp, fineract = _clients()
    try:
        status = sync_one(
            get_engine(), erp, fineract, reference, correlation_id=g.get("correlation_id")
        )
    except SyncInProgressError:
        current = next(
            (a for a in get_report(get_engine()).accounts if a.erp_reference == reference), None
        )
        return AccountSyncResponse.from_status(current, in_progress=True).model_dump(), 202
    except LookupError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, f"The ERP has no account {reference}.", 404) from exc
    except ErpError as exc:
        raise _erp_api_error(exc) from exc
    finally:
        erp.close()
        fineract.close()
    return AccountSyncResponse.from_status(status, in_progress=False).model_dump(), 200


@bp.get(
    "/api/accounts/sync",
    summary="GL sync state",
    description="The state of every ERP account known to the sync. Calls nothing.",
    responses={200: SyncReportResponse},
)
def sync_report() -> tuple[dict[str, Any], int]:
    return SyncReportResponse.from_report(get_report(get_engine())).model_dump(), 200


def _clients() -> tuple[ErpClient, FineractClient]:
    settings = get_settings()
    erp = ErpClient(settings.erp_base_url, settings.erp_api_key, settings.erp_timeout_seconds)
    fineract = FineractClient(
        base_url=settings.fineract_base_url,
        tenant=settings.fineract_tenant,
        username=settings.fineract_username,
        password=settings.fineract_password,
        timeout_seconds=settings.fineract_timeout_seconds,
    )
    return erp, fineract


def _erp_api_error(exc: ErpError) -> ApiError:
    if isinstance(exc, ErpTimeout):
        return ApiError(ErrorCode.ERP_TIMEOUT, f"The ERP did not answer: {exc.message}", 504)
    if isinstance(exc, ErpUnavailable):
        return ApiError(ErrorCode.ERP_UNAVAILABLE, f"The ERP is not reachable: {exc.message}", 503)
    return ApiError(ErrorCode.ERP_UNAVAILABLE, f"Could not read the ERP chart: {exc.message}", 502)
