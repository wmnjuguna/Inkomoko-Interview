"""One JSON error format for every failure:
{"error": {"code", "message", "details", "correlation_id"}}."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from flask import Flask, Response, g, jsonify
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNKNOWN_ACCOUNT = "unknown_account"
    HEADER_ACCOUNT = "header_account"
    ENTRY_NOT_BALANCED = "entry_not_balanced"
    REFERENCE_CONFLICT = "reference_conflict"
    CODE_TAKEN = "code_taken"
    PARENT_INVALID = "parent_invalid"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    ERP_UNAVAILABLE = "erp_unavailable"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"


class ErpError(Exception):
    """A rule was broken. Services raise it; the app turns it into the JSON error format."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status: int,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        self.headers = headers or {}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "correlation_id": g.get("correlation_id"),
        }
    }


def error_response(err: ErpError) -> Response:
    response = jsonify(error_body(err.code, err.message, err.details))
    response.status_code = err.status
    response.headers.update(err.headers)
    return response


def validation_error_response(exc: ValidationError) -> Response:
    """Used by flask-openapi3 when a request does not match its Pydantic model."""
    problems = [
        {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()
    ]
    first = problems[0] if problems else {"field": "", "problem": "invalid"}
    message = f"{first['field'] or 'body'}: {first['problem']}"
    response = jsonify(error_body(ErrorCode.INVALID_REQUEST, message, {"problems": problems}))
    response.status_code = 422
    return response


def register_error_handlers(app: Flask) -> None:
    app.register_error_handler(ErpError, error_response)

    def http_error(exc: HTTPException) -> Response:
        code = ErrorCode.NOT_FOUND if exc.code == 404 else ErrorCode.INVALID_REQUEST
        response = jsonify(error_body(code, exc.description or exc.name))
        response.status_code = exc.code or 500
        return response

    app.register_error_handler(HTTPException, http_error)
