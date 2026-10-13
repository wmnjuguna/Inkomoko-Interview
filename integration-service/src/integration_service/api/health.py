"""GET /health: is the database reachable."""

from __future__ import annotations

from typing import Any, Literal

from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from integration_service.db import get_engine


class Health(BaseModel):
    status: Literal["UP", "DOWN"]
    database: Literal["ok", "unreachable"]


bp = APIBlueprint("health", __name__, abp_tags=[Tag(name="health")])


@bp.get("/health", summary="Database reachable", responses={200: Health, 503: Health})
def health() -> tuple[dict[str, Any], int]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return {"status": "DOWN", "database": "unreachable"}, 503
    return {"status": "UP", "database": "ok"}, 200
