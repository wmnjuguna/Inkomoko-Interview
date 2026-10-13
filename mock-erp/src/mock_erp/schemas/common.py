"""Models shared by every area."""

from __future__ import annotations

from http import HTTPStatus
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


# The shape flask-openapi3 expects for a blueprint's shared responses.
Responses = dict[str | int | HTTPStatus, type[BaseModel] | dict[Any, Any] | None]

# Answers every /admin call can give besides its own: no key, or a body that doesn't fit.
ADMIN_ERRORS: Responses = {401: ErrorResponse, 422: ErrorResponse}

# /api calls can also meet the fault switch: 429, 503 and 504.
API_ERRORS: Responses = {
    **ADMIN_ERRORS,
    429: ErrorResponse,
    503: ErrorResponse,
    504: ErrorResponse,
}
