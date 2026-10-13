"""GET /api/transactions/{external_reference}: what happened to a request.

Shows every operation the calling partner sent with this reference (one per type: a client,
an application, an approval, a disbursement, a repayment), with its status, the ERP status
and the full status history. Another partner's operations are not shown: for them the
reference is 404. Calls nothing outside our database, and shows no Fineract ids."""

from __future__ import annotations

from typing import Any

from flask import g
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from integration_service.db import get_engine
from integration_service.errors import ApiError, ErrorCode
from integration_service.logs import log_field
from integration_service.schemas.common import API_ERRORS, ErrorResponse
from integration_service.schemas.operations import (
    OperationStatus,
    TransactionPath,
    TransactionResponse,
)
from integration_service.services.operations import list_for_reference

bp = APIBlueprint(
    "transactions",
    __name__,
    abp_tags=[Tag(name="transactions")],
    abp_security=[{"api_key": []}],
    abp_responses=API_ERRORS,
)


@bp.get(
    "/api/transactions/<external_reference>",
    summary="Status and history of one request",
    description="Every operation you sent with this external_reference: its type, status, "
    "ERP status, attempts, last error and status history. Use it to learn the live status "
    "of a request whose repeat returned the saved first answer.",
    responses={200: TransactionResponse, 404: ErrorResponse},
)
def get_transaction(path: TransactionPath) -> tuple[dict[str, Any], int]:
    reference = path.external_reference
    log_field("external_reference", reference)
    views = list_for_reference(get_engine(), reference, g.partner)
    if not views:
        raise ApiError(ErrorCode.NOT_FOUND, f"No request with external_reference {reference}.", 404)
    for name in ("client_uid", "loan_reference"):
        values = {getattr(v, name) for v in views} - {None}
        if len(values) == 1:
            log_field(name, values.pop())
    if len(views) == 1:
        log_field("status", views[0].status)
    body = TransactionResponse(
        external_reference=reference,
        operations=[OperationStatus.from_view(v) for v in views],
    )
    return body.model_dump(mode="json", by_alias=True), 200
