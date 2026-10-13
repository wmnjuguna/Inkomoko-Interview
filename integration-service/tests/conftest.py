"""Shared fixtures. Unit tests need nothing. Integration tests use a real Postgres:
INTEGRATION_TEST_DATABASE_URL, by default the integration_test database from docker compose.
Fineract and the Mock ERP URLs come from env vars, with the local compose ports as defaults."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from flask.testing import FlaskClient
from flask_openapi3.openapi import OpenAPI
from sqlalchemy import Engine, create_engine, text

from integration_service.app import create_app
from integration_service.config import Settings

PARTNER_A = "partner-a"
PARTNER_A_KEY = "test-key-a"
PARTNER_B = "partner-b"
PARTNER_B_KEY = "test-key-b"
DEFAULT_TEST_DB = (
    "postgresql+psycopg://integration:change-me-integration@localhost:5432/integration_test"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("INTEGRATION_TEST_DATABASE_URL", DEFAULT_TEST_DB)


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """A fresh schema, built by the real migrations, once per test run."""
    eng = create_engine(database_url, pool_size=20, max_overflow=10)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        cfg = Config(os.path.join(ROOT, "alembic.ini"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


def reset_database(engine: Engine) -> None:
    """Empty every table the service owns."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE gl_sync"))


@pytest.fixture
def db(engine: Engine) -> Engine:
    reset_database(engine)
    return engine


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        partner_api_keys={PARTNER_A: PARTNER_A_KEY, PARTNER_B: PARTNER_B_KEY},
        fineract_base_url=os.environ.get(
            "FINERACT_BASE_URL", "http://localhost:8080/fineract-provider/api/v1"
        ),
        fineract_tenant=os.environ.get("FINERACT_TENANT", "default"),
        fineract_username=os.environ.get("FINERACT_USERNAME", "mifos"),
        fineract_password=os.environ.get("FINERACT_PASSWORD", "password"),
        erp_base_url=os.environ.get("ERP_BASE_URL", "http://localhost:8090"),
        erp_api_key=os.environ.get("ERP_API_KEY", "change-me-erp-key"),
    )


@pytest.fixture
def app(settings: Settings, db: Engine) -> OpenAPI:
    application = create_app(settings, engine=db)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app: OpenAPI) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-API-Key": PARTNER_A_KEY}


@pytest.fixture
def auth_b() -> dict[str, str]:
    return {"X-API-Key": PARTNER_B_KEY}
