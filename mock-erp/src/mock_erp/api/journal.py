"""/api/journal-entries: post an entry (idempotent by external_reference), read and search."""

from __future__ import annotations

from typing import Any

from flask import g
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from mock_erp.db import get_engine
from mock_erp.logs import log_field
from mock_erp.schemas.common import API_ERRORS, ErrorResponse
from mock_erp.schemas.journal import (
    JournalEntryIn,
    JournalEntryList,
    JournalEntryOut,
    JournalPostOut,
    JournalSearchQuery,
    ReferencePath,
)
from mock_erp.services import journal as service

bp = APIBlueprint(
    "journal",
    __name__,
    abp_tags=[Tag(name="journal")],
    abp_security=[{"api_key": []}],
    abp_responses=API_ERRORS,
)


@bp.post(
    "/api/journal-entries",
    summary="Post a journal entry",
    description="201 for a new entry, 200 for a retry with the same content, "
    "409 when the reference is stored with different content.",
    responses={
        201: JournalPostOut,
        200: JournalPostOut,
        401: ErrorResponse,
        409: ErrorResponse,
        422: ErrorResponse,
    },
)
def post_entry(body: JournalEntryIn) -> tuple[dict[str, Any], int]:
    log_field("external_reference", body.external_reference)
    result = service.post_entry(get_engine(), body, g.get("correlation_id"))
    log_field("erp_transaction_id", str(result.erp_transaction_id))
    return result.model_dump(mode="json"), 201 if result.status == "CREATED" else 200


@bp.get(
    "/api/journal-entries/<external_reference>",
    summary="Read one journal entry",
    responses={200: JournalEntryOut, 401: ErrorResponse, 404: ErrorResponse},
)
def get_entry(path: ReferencePath) -> tuple[dict[str, Any], int]:
    log_field("external_reference", path.external_reference)
    entry = service.get_entry(get_engine(), path.external_reference)
    log_field("erp_transaction_id", str(entry.erp_transaction_id))
    return entry.model_dump(mode="json"), 200


@bp.get(
    "/api/journal-entries",
    summary="Search journal entries",
    description="Newest first. Repeat reference to fetch many entries at once.",
    responses={200: JournalEntryList, 401: ErrorResponse, 422: ErrorResponse},
)
def search_entries(query: JournalSearchQuery) -> tuple[dict[str, Any], int]:
    items = service.search_entries(
        get_engine(),
        date_from=query.date_from,
        date_to=query.date_to,
        references=query.reference,
        source=query.source,
        q=query.q,
        limit=query.limit,
    )
    return JournalEntryList(items=items).model_dump(mode="json"), 200
