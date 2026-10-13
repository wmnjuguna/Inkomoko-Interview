"""The shared plumbing: settings, health, partner keys, correlation id, error format,
JSON logs, OpenAPI and the first migration."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from flask import g
from flask.testing import FlaskClient
from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.openapi import OpenAPI
from pydantic import BaseModel, Field
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from integration_service.app import create_app
from integration_service.config import Settings
from integration_service.logs import JsonFormatter, log_event, log_field, logger
from integration_service.models import gl_sync
from integration_service.schemas.common import API_ERRORS


class ProbeBody(BaseModel):
    external_reference: str = Field(min_length=1, max_length=40)
    amount: int


def _probe_blueprint() -> APIBlueprint:
    """Test-only routes that use the real plumbing: validation, log fields, a crash."""
    bp = APIBlueprint("probe", __name__, abp_security=[{"api_key": []}], abp_responses=API_ERRORS)

    @bp.post("/api/_probe")
    def probe(body: ProbeBody) -> dict[str, Any]:
        log_field("external_reference", body.external_reference)
        log_field("client_uid", "SME-000001")
        log_field("loan_reference", "LN-2026-000001")
        log_field("fineract_transaction_id", "991")
        log_field("erp_transaction_id", "JE-1")
        log_field("status", "SYNCED")
        return {"partner": g.partner}

    @bp.get("/api/_crash")
    def crash() -> dict[str, Any]:
        raise RuntimeError("boom")

    return bp


@pytest.fixture
def probe_app(app: OpenAPI) -> OpenAPI:
    app.register_api(_probe_blueprint())
    return app


@pytest.fixture
def probe_client(probe_app: OpenAPI) -> FlaskClient:
    return probe_app.test_client()


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def log_lines(app: OpenAPI) -> Iterator[list[dict[str, Any]]]:
    handler = _Collect()
    logger.addHandler(handler)
    yield handler.lines
    logger.removeHandler(handler)


# --- settings -------------------------------------------------------------------------------

ENV = {
    "INTEGRATION_DATABASE_URL": "postgresql+psycopg://u:p@db:5432/integration_db",
    "PARTNER_API_KEYS": " demo-partner:key-1 , other:key-2 ",
    "FINERACT_BASE_URL": "http://fineract:8080/fineract-provider/api/v1/",
    "FINERACT_USERNAME": "svc",
    "FINERACT_PASSWORD": "secret",
    "ERP_BASE_URL": "http://mock-erp:8090",
    "ERP_API_KEY": "erp-key",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for name in (*ENV, "FINERACT_TENANT", "FINERACT_TIMEOUT_SECONDS", "ERP_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    for name, value in {**ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_settings_read_the_environment_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    s = Settings.from_env()
    assert s.database_url == ENV["INTEGRATION_DATABASE_URL"]
    assert s.partner_api_keys == {"demo-partner": "key-1", "other": "key-2"}
    assert s.fineract_base_url == "http://fineract:8080/fineract-provider/api/v1"
    assert (s.fineract_tenant, s.fineract_username, s.fineract_password) == (
        "default",
        "svc",
        "secret",
    )
    assert (s.fineract_timeout_seconds, s.erp_timeout_seconds) == (30.0, 10.0)
    assert (s.erp_base_url, s.erp_api_key, s.log_level) == (
        "http://mock-erp:8090",
        "erp-key",
        "INFO",
    )


def test_settings_take_optional_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        FINERACT_TENANT="t2",
        FINERACT_TIMEOUT_SECONDS="5",
        ERP_TIMEOUT_SECONDS="2.5",
        LOG_LEVEL="debug",
    )
    s = Settings.from_env()
    assert (s.fineract_tenant, s.fineract_timeout_seconds) == ("t2", 5.0)
    assert (s.erp_timeout_seconds, s.log_level) == (2.5, "DEBUG")


@pytest.mark.parametrize(
    "keys", ["", "  ", "no-colon", "name:", ":key", "a:k,a:k2", "a:same,b:same"]
)
def test_settings_refuse_bad_partner_keys(monkeypatch: pytest.MonkeyPatch, keys: str) -> None:
    _set_env(monkeypatch, PARTNER_API_KEYS=keys)
    with pytest.raises(RuntimeError, match="PARTNER_API_KEYS"):
        Settings.from_env()


def test_settings_need_the_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("INTEGRATION_DATABASE_URL")
    with pytest.raises(RuntimeError, match="INTEGRATION_DATABASE_URL"):
        Settings.from_env()


# --- health ---------------------------------------------------------------------------------


def test_health_reports_database_up(client: FlaskClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "UP", "database": "ok"}


def test_health_reports_database_down(settings: Settings) -> None:
    dead = create_engine("postgresql+psycopg://nobody:x@127.0.0.1:1/none")
    response = create_app(settings, engine=dead).test_client().get("/health")
    assert response.status_code == 503
    assert response.json == {"status": "DOWN", "database": "unreachable"}


# --- partner keys ---------------------------------------------------------------------------


def test_api_without_key_is_refused_in_the_error_format(client: FlaskClient) -> None:
    response = client.post("/api/accounts/sync", headers={"X-Correlation-ID": "c-test-1"})
    assert response.status_code == 401
    assert response.json == {
        "error": {
            "code": "UNAUTHENTICATED",
            "message": "Missing or wrong X-API-Key.",
            "correlation_id": "c-test-1",
            "details": {},
        }
    }


def test_wrong_key_is_refused_for_admin_too(client: FlaskClient) -> None:
    response = client.post("/admin/recover", headers={"X-API-Key": "nope"})
    assert response.status_code == 401
    assert response.json["error"]["code"] == "UNAUTHENTICATED"


def test_each_partner_key_names_its_partner(
    probe_client: FlaskClient, auth: dict[str, str], auth_b: dict[str, str]
) -> None:
    body = {"external_reference": "GL-1200", "amount": 1}
    a = probe_client.post("/api/_probe", json=body, headers=auth)
    b = probe_client.post("/api/_probe", json=body, headers=auth_b)
    assert (a.status_code, a.json) == (200, {"partner": "partner-a"})
    assert (b.status_code, b.json) == (200, {"partner": "partner-b"})


def test_health_and_openapi_need_no_key(client: FlaskClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/openapi/openapi.json").status_code == 200


# --- correlation id and errors ----------------------------------------------------------------


def test_correlation_id_is_echoed_or_created(client: FlaskClient) -> None:
    sent = client.get("/health", headers={"X-Correlation-ID": "c-abc"})
    assert sent.headers["X-Correlation-ID"] == "c-abc"
    made = client.get("/health")
    assert made.headers["X-Correlation-ID"].startswith("c-")
    assert len(made.headers["X-Correlation-ID"]) > 10


def test_correlation_id_is_on_error_answers_too(client: FlaskClient) -> None:
    response = client.get("/api/nowhere", headers={"X-Correlation-ID": "c-err"})
    assert response.headers["X-Correlation-ID"] == "c-err"


def test_unknown_path_uses_the_error_format(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get("/api/nowhere", headers={**auth, "X-Correlation-ID": "c-404"})
    assert response.status_code == 404
    assert response.json["error"]["code"] == "NOT_FOUND"
    assert response.json["error"]["correlation_id"] == "c-404"
    assert set(response.json["error"]) == {"code", "message", "correlation_id", "details"}


def test_bad_body_gets_422_with_each_problem(
    probe_client: FlaskClient, auth: dict[str, str]
) -> None:
    response = probe_client.post(
        "/api/_probe", json={"external_reference": "", "amount": "lots"}, headers=auth
    )
    assert response.status_code == 422
    error = response.json["error"]
    assert error["code"] == "VALIDATION_ERROR"
    fields = {p["field"] for p in error["details"]["problems"]}
    assert fields == {"external_reference", "amount"}


def test_a_crash_still_answers_in_the_error_format(
    probe_client: FlaskClient, auth: dict[str, str]
) -> None:
    response = probe_client.get("/api/_crash", headers={**auth, "X-Correlation-ID": "c-500"})
    assert response.status_code == 500
    assert response.json["error"]["code"] == "INTERNAL_ERROR"
    assert response.json["error"]["correlation_id"] == "c-500"
    assert "boom" not in response.json["error"]["message"]


# --- JSON logs ------------------------------------------------------------------------------


def test_request_log_line_has_the_section_9_fields(
    probe_client: FlaskClient, auth: dict[str, str], log_lines: list[dict[str, Any]]
) -> None:
    probe_client.post(
        "/api/_probe",
        json={"external_reference": "GL-1200", "amount": 1},
        headers={**auth, "X-Correlation-ID": "c-log"},
    )
    line = next(item for item in log_lines if item.get("path") == "/api/_probe")
    assert line["correlation_id"] == "c-log"
    assert line["method"] == "POST"
    assert line["partner"] == "partner-a"
    assert line["http_status"] == 200
    assert line["level"] == "INFO"
    assert "timestamp" in line
    assert isinstance(line["duration_ms"], float)
    assert line["external_reference"] == "GL-1200"
    assert line["client_uid"] == "SME-000001"
    assert line["loan_reference"] == "LN-2026-000001"
    assert line["fineract_transaction_id"] == "991"
    assert line["erp_transaction_id"] == "JE-1"
    assert line["status"] == "SYNCED"


def test_plain_request_log_line_uses_http_status(
    client: FlaskClient, log_lines: list[dict[str, Any]]
) -> None:
    client.get("/health")
    line = next(item for item in log_lines if item.get("path") == "/health")
    assert line["status"] == 200
    assert line["partner"] is None


def test_background_event_log_has_the_same_shape(log_lines: list[dict[str, Any]]) -> None:
    log_event("gl_sync.retry", correlation_id="c-bg", external_reference="GL-1200", status="FAILED")
    line = log_lines[-1]
    assert line["message"] == "gl_sync.retry"
    assert line["correlation_id"] == "c-bg"
    assert line["external_reference"] == "GL-1200"
    assert line["status"] == "FAILED"
    assert line["level"] == "INFO"
    assert "timestamp" in line


# --- OpenAPI --------------------------------------------------------------------------------


def test_openapi_spec_is_generated(client: FlaskClient) -> None:
    response = client.get("/openapi/openapi.json")
    assert response.status_code == 200
    assert response.json["info"]["title"] == "Integration Service"
    assert "/health" in response.json["paths"]
    assert response.json["components"]["securitySchemes"]["api_key"]["name"] == "X-API-Key"


def test_spec_shows_the_real_error_shape_for_401_and_422(probe_client: FlaskClient) -> None:
    spec = probe_client.get("/openapi/openapi.json").json
    responses = spec["paths"]["/api/_probe"]["post"]["responses"]
    for status in ("401", "422"):
        ref = responses[status]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")
    assert "ValidationErrorModel" not in json.dumps(spec)
    assert {"200", "503"} <= set(spec["paths"]["/health"]["get"]["responses"])


# --- migrations -----------------------------------------------------------------------------


def test_migrations_create_the_gl_sync_table(db: Engine) -> None:
    columns = {c["name"] for c in inspect(db).get_columns("gl_sync")}
    assert columns == {c.name for c in gl_sync.columns}
    assert columns == {
        "erp_reference",
        "erp_code",
        "name",
        "erp_type",
        "erp_usage",
        "erp_parent_reference",
        "payload_hash",
        "status",
        "fineract_gl_id",
        "attempts",
        "last_error",
        "last_attempt_at",
        "synced_at",
        "created_at",
        "updated_at",
    }
    with db.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0001"


def _insert(db: Engine, **overrides: Any) -> None:
    row = {
        "erp_reference": "GL-1200",
        "erp_code": "1200",
        "name": "Loans",
        "erp_type": "ASSET",
        "erp_usage": "DETAIL",
        "payload_hash": "h",
        "status": "SYNC_PENDING",
        **overrides,
    }
    with db.begin() as conn:
        conn.execute(gl_sync.insert().values(**row))


def test_gl_sync_defaults_and_constraints(db: Engine) -> None:
    _insert(db)
    with db.connect() as conn:
        row = conn.execute(gl_sync.select()).mappings().one()
    assert row["attempts"] == 0
    assert row["created_at"] is not None and row["updated_at"] is not None
    assert row["fineract_gl_id"] is None
    with pytest.raises(IntegrityError, match="gl_sync_status_check"):
        _insert(db, erp_reference="GL-1300", erp_code="1300", status="DONE")
    with pytest.raises(IntegrityError, match="gl_sync_erp_code_key"):
        _insert(db, erp_reference="GL-1300")  # same erp_code 1200
    with pytest.raises(IntegrityError, match="gl_sync_pkey"):
        _insert(db, erp_code="1300")  # same erp_reference
