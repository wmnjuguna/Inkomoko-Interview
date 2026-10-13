"""POST /admin/recover: run one recovery pass now, and show what it did.

The same pass the background loop runs: requests stuck in FINERACT_PENDING are looked up in
Fineract by their externalId and finished, failed or left for later, then due journal
entries are sent to the ERP. The answer holds counts only, never ids. Not for partners: it
sits under /admin, which needs an API key like /api."""

from __future__ import annotations

from typing import Any

from flask import current_app
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag
from pydantic import BaseModel

from integration_service.config import get_settings
from integration_service.db import get_engine
from integration_service.logs import log_field
from integration_service.schemas.common import API_ERRORS
from integration_service.services.recovery import gl_role_accounts_from, recover_with_settings

ACCOUNTS_KEY = "integration_service.gl_role_accounts"

bp = APIBlueprint(
    "admin",
    __name__,
    abp_tags=[Tag(name="admin")],
    abp_security=[{"api_key": []}],
    abp_responses=API_ERRORS,
)


class OperationCounts(BaseModel):
    checked: int
    completed: int
    needs_reconciliation: int
    failed: int
    still_unknown: int
    skipped_busy: int
    finished_meanwhile: int


class OutboxCounts(BaseModel):
    claimed: int
    sent: int
    retried: int
    rate_limited: int
    needs_reconciliation: int
    stale: int


class RecoverResponse(BaseModel):
    operations: OperationCounts
    outbox: OutboxCounts


@bp.post(
    "/admin/recover",
    summary="Re-check unfinished requests now",
    description="Looks up every request still waiting on Fineract (older than the stale "
    "limit, or due again) by its externalId and finishes, fails or keeps it; then sends due "
    "journal entries to the ERP. Counts only. Not for partners.",
    responses={200: RecoverResponse},
)
def recover() -> tuple[dict[str, Any], int]:
    settings = get_settings()
    accounts = current_app.extensions.get(ACCOUNTS_KEY) or gl_role_accounts_from(
        settings.gl_roles_config_path
    )
    report = recover_with_settings(settings, get_engine(), accounts)
    log_field("status", "RECOVERY_PASS")
    return RecoverResponse.model_validate(report.as_dict()).model_dump(), 200
