"""/admin routes: the fault switch."""

from __future__ import annotations

from typing import Any

from flask import current_app
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from mock_erp import faults
from mock_erp.db import get_engine
from mock_erp.schemas.common import ErrorResponse
from mock_erp.schemas.faults import FaultSettingIn, FaultSettingOut

bp = APIBlueprint("admin", __name__, abp_tags=[Tag(name="admin")], abp_security=[{"api_key": []}])


@bp.get(
    "/admin/faults",
    summary="Current fault setting",
    responses={200: FaultSettingOut, 401: ErrorResponse},
)
def get_faults() -> tuple[dict[str, Any], int]:
    return faults.get_setting(get_engine()).model_dump(), 200


@bp.put(
    "/admin/faults",
    summary="Turn a fault on",
    description="Paths under path_prefix fail on purpose until cleared or until "
    "remaining calls have failed. /health, /admin, /ui and /openapi never fail.",
    responses={200: FaultSettingOut, 401: ErrorResponse, 422: ErrorResponse},
)
def put_faults(body: FaultSettingIn) -> tuple[dict[str, Any], int]:
    delay = body.delay_seconds or faults.default_delay(current_app.config["ERP_TIMEOUT_DELAY"])
    setting = faults.set_fault(
        get_engine(),
        body.mode,
        path_prefix=body.path_prefix,
        delay_seconds=delay,
        remaining=body.remaining,
        retry_after=body.retry_after,
    )
    return setting.model_dump(), 200


@bp.delete(
    "/admin/faults",
    summary="Turn the fault off",
    responses={200: FaultSettingOut, 401: ErrorResponse},
)
def delete_faults() -> tuple[dict[str, Any], int]:
    return faults.clear_fault(get_engine()).model_dump(), 200
