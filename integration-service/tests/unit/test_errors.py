"""The error format and codes, checked without a database."""

from __future__ import annotations

from flask import Flask, g
from pydantic import BaseModel, ValidationError

from integration_service.errors import (
    ApiError,
    ErrorCode,
    error_body,
    error_response,
    validation_error_response,
)


class Sample(BaseModel):
    name: str
    amount: int


def test_error_codes_are_upper_case_and_cover_the_api_doc() -> None:
    expected = {
        "VALIDATION_ERROR",
        "UNAUTHENTICATED",
        "NOT_FOUND",
        "IDEMPOTENCY_CONFLICT",
        "CLIENT_UID_TAKEN",
        "LOAN_ALREADY_DISBURSED",
        "GL_CODE_TAKEN",
        "FINERACT_UNAVAILABLE",
        "FINERACT_TIMEOUT",
    }
    values = {code.value for code in ErrorCode}
    assert expected <= values
    assert all(value == value.upper() for value in values)


def test_api_error_keeps_its_parts() -> None:
    err = ApiError(ErrorCode.GL_CODE_TAKEN, "taken", 409, {"gl_code": "1200"})
    assert (err.code, err.message, err.status, err.details) == (
        "GL_CODE_TAKEN",
        "taken",
        409,
        {"gl_code": "1200"},
    )
    assert ApiError(ErrorCode.NOT_FOUND, "x", 404).details == {}


def test_error_body_carries_the_correlation_id() -> None:
    with Flask(__name__).test_request_context():
        g.correlation_id = "c-1"
        assert error_body(ErrorCode.NOT_FOUND, "No such thing.") == {
            "error": {
                "code": "NOT_FOUND",
                "message": "No such thing.",
                "correlation_id": "c-1",
                "details": {},
            }
        }


def test_error_response_uses_the_status_and_headers() -> None:
    with Flask(__name__).test_request_context():
        err = ApiError(ErrorCode.FINERACT_UNAVAILABLE, "down", 503, headers={"Retry-After": "5"})
        response = error_response(err)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json is not None
    assert response.json["error"]["code"] == "FINERACT_UNAVAILABLE"


def test_validation_error_lists_each_problem() -> None:
    try:
        Sample.model_validate({"amount": "lots"})
    except ValidationError as exc:
        with Flask(__name__).test_request_context():
            response = validation_error_response(exc)
    assert response.status_code == 422
    body = response.json
    assert body is not None
    assert body["error"]["code"] == "VALIDATION_ERROR"
    fields = {p["field"] for p in body["error"]["details"]["problems"]}
    assert fields == {"name", "amount"}
    assert body["error"]["message"].startswith("name:")
