"""POST /api/clients and GET /api/clients/{client_uid} (Part C). The work is in
services/clients.py. Answers carry our identifiers only, never Fineract ids."""

from __future__ import annotations

from typing import Any

from flask import g
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from integration_service.config import get_settings
from integration_service.db import get_engine
from integration_service.logs import log_field
from integration_service.schemas.clients import (
    ClientCheckResponse,
    ClientPath,
    ClientQuery,
    ClientResponse,
    CreateClientRequest,
)
from integration_service.schemas.common import API_ERRORS, UPSTREAM_ERRORS, ErrorResponse
from integration_service.schemas.loans import AcceptedResponse
from integration_service.services.clients import check_client, create_client
from integration_service.services.loans import fineract_from_settings

bp = APIBlueprint(
    "clients",
    __name__,
    abp_tags=[Tag(name="clients")],
    abp_security=[{"api_key": []}],
    abp_responses=API_ERRORS,
)


@bp.post(
    "/api/clients",
    summary="Create a client",
    description="Creates the client in Fineract, active from today, keyed by client_uid. "
    "201 the first time. The same external_reference and data again gives 200 with the same "
    "body; with other data, 409 IDEMPOTENCY_CONFLICT. A client_uid already used by another "
    "reference gives 409 CLIENT_UID_TAKEN.",
    responses={
        201: ClientResponse,
        200: ClientResponse,
        202: AcceptedResponse,
        409: ErrorResponse,
        **UPSTREAM_ERRORS,
    },
)
def post_client(body: CreateClientRequest) -> tuple[dict[str, Any], int]:
    log_field("external_reference", body.external_reference)
    log_field("client_uid", body.client_uid)
    fineract = fineract_from_settings(get_settings())
    try:
        answer = create_client(get_engine(), fineract, body, g.partner, g.get("correlation_id"))
    finally:
        fineract.close()
    log_field("status", answer.body.get("status"))
    return answer.body, answer.status


@bp.get(
    "/api/clients/<client_uid>",
    summary="Check a client",
    description="Reads Fineract: does the client exist, is it active, and does it have the "
    "name we recorded? Add ?name= to check the name you expect.",
    responses={200: ClientCheckResponse, 404: ErrorResponse, **UPSTREAM_ERRORS},
)
def get_client(path: ClientPath, query: ClientQuery) -> tuple[dict[str, Any], int]:
    log_field("client_uid", path.client_uid)
    fineract = fineract_from_settings(get_settings())
    try:
        body = check_client(
            get_engine(), fineract, path.client_uid, query.name, g.get("correlation_id")
        )
    finally:
        fineract.close()
    log_field("status", body["status"])
    return body, 200
