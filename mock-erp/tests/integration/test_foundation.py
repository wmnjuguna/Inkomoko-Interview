"""The shared plumbing: health, API key, correlation id, error format, OpenAPI."""

from __future__ import annotations

from flask.testing import FlaskClient
from sqlalchemy import Engine, text


def test_health_reports_database_up(client: FlaskClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "UP", "database": "ok"}


def test_api_without_key_is_refused_in_the_error_format(client: FlaskClient) -> None:
    response = client.get("/api/accounts", headers={"X-Correlation-ID": "c-test-1"})
    assert response.status_code == 401
    assert response.json == {
        "error": {
            "code": "unauthorized",
            "message": "Missing or wrong X-API-Key.",
            "details": {},
            "correlation_id": "c-test-1",
        }
    }


def test_wrong_key_is_refused_for_admin_too(client: FlaskClient) -> None:
    response = client.get("/admin/faults", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_correlation_id_is_echoed_or_created(client: FlaskClient) -> None:
    sent = client.get("/health", headers={"X-Correlation-ID": "c-abc"})
    assert sent.headers["X-Correlation-ID"] == "c-abc"
    made = client.get("/health")
    assert made.headers["X-Correlation-ID"].startswith("c-")


def test_unknown_path_uses_the_error_format(client: FlaskClient) -> None:
    response = client.get("/nowhere")
    assert response.status_code == 404
    assert response.json["error"]["code"] == "not_found"


def test_openapi_spec_is_generated(client: FlaskClient) -> None:
    response = client.get("/openapi/openapi.json")
    assert response.status_code == 200
    assert response.json["info"]["title"] == "Mock ERP"
    assert "/health" in response.json["paths"]


def test_migrations_seed_the_chart(db: Engine) -> None:
    with db.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM accounts")).scalar_one()
    assert count == 13


def test_spec_describes_the_real_422_and_health_answers(client: FlaskClient) -> None:
    spec = client.get("/openapi/openapi.json").json
    post = spec["paths"]["/api/journal-entries"]["post"]["responses"]
    ref = post["422"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ErrorResponse")
    assert "ValidationErrorModel" not in str(spec)
    health = spec["paths"]["/health"]["get"]["responses"]
    assert {"200", "503"} <= set(health)
