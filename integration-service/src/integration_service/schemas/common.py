"""Models shared by every area."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    correlation_id: str | None
    details: dict[str, Any]


class ErrorResponse(BaseModel):
    """Every error has this shape."""

    error: ErrorDetail


# The shape flask-openapi3 expects for a blueprint's shared responses.
Responses = dict[str | int | HTTPStatus, type[BaseModel] | dict[Any, Any] | None]

# Answers every /api and /admin call can give besides its own: no key, or a bad body.
API_ERRORS: Responses = {401: ErrorResponse, 422: ErrorResponse}

# Answers a call can give when Fineract or the ERP fails or is slow.
UPSTREAM_ERRORS: Responses = {502: ErrorResponse, 503: ErrorResponse, 504: ErrorResponse}
