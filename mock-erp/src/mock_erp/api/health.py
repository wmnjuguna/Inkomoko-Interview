"""GET /health: is the database reachable."""

from __future__ import annotations

from typing import Any

from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from mock_erp.db import get_engine

bp = APIBlueprint("health", __name__, abp_tags=[Tag(name="health")])


@bp.get("/health", summary="Database reachable")
def health() -> tuple[dict[str, Any], int]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return {"status": "DOWN", "database": "unreachable"}, 503
    return {"status": "UP", "database": "ok"}, 200
