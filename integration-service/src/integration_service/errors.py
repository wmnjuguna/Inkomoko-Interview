"""One JSON error format for every failure:
{"error": {"code", "message", "correlation_id", "details"}}."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from flask import Flask, Response, g, jsonify
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from integration_service.logs import logger


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    NOT_FOUND = "NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CLIENT_UID_TAKEN = "CLIENT_UID_TAKEN"
    LOAN_ALREADY_DISBURSED = "LOAN_ALREADY_DISBURSED"
    GL_CODE_TAKEN = "GL_CODE_TAKEN"
    FINERACT_UNAVAILABLE = "FINERACT_UNAVAILABLE"
    FINERACT_TIMEOUT = "FINERACT_TIMEOUT"
    ERP_UNAVAILABLE = "ERP_UNAVAILABLE"
    ERP_TIMEOUT = "ERP_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(Exception):
    """A rule was broken or a call failed. The app turns it into the JSON error format."""

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
            "correlation_id": g.get("correlation_id"),
            "details": details or {},
        }
    }


def error_response(err: ApiError) -> Response:
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
    response = jsonify(error_body(ErrorCode.VALIDATION_ERROR, message, {"problems": problems}))
    response.status_code = 422
    return response


def register_error_handlers(app: Flask) -> None:
    app.register_error_handler(ApiError, error_response)

    def http_error(exc: HTTPException) -> Response:
        status = exc.code or 500
        if status == 404:
            code = ErrorCode.NOT_FOUND
        elif status < 500:
            code = ErrorCode.VALIDATION_ERROR
        else:
            code = ErrorCode.INTERNAL_ERROR
        response = jsonify(error_body(code, exc.description or exc.name))
        response.status_code = status
        return response

    app.register_error_handler(HTTPException, http_error)

    def unexpected_error(exc: Exception) -> Response:
        """A bug or an unhandled failure: log it in full, answer without the inner details."""
        fields = {"correlation_id": g.get("correlation_id"), "error": type(exc).__name__}
        logger.exception("unhandled error", extra={"fields": fields})
        response = jsonify(
            error_body(ErrorCode.INTERNAL_ERROR, "Something went wrong on our side.")
        )
        response.status_code = 500
        return response

    app.register_error_handler(Exception, unexpected_error)
