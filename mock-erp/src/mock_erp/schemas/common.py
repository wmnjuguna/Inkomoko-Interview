"""Models shared by every area."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any]
    correlation_id: str | None


class ErrorResponse(BaseModel):
    """Every error has this shape."""

    error: ErrorDetail
