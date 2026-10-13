"""Shared fixtures. Unit tests need nothing. Integration tests use a real Postgres:
ERP_TEST_DATABASE_URL, by default the erp_test database from docker compose."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from flask.testing import FlaskClient
from flask_openapi3.openapi import OpenAPI
from sqlalchemy import Engine, create_engine, text

from mock_erp.app import create_app
from mock_erp.config import Settings
from mock_erp.hashing import request_hash
from mock_erp.seed import SEED_ACCOUNTS

TEST_API_KEY = "test-key"
DEFAULT_TEST_DB = "postgresql+psycopg://erp:change-me-erp@localhost:5432/erp_test"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("ERP_TEST_DATABASE_URL", DEFAULT_TEST_DB)


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
    """Back to the seeded chart, an empty journal and no fault."""
    with engine.begin() as conn:
        conn.execute(
            text("TRUNCATE journal_lines, journal_entries, accounts RESTART IDENTITY CASCADE")
        )
        for account in SEED_ACCOUNTS:
            conn.execute(
                text(
                    "INSERT INTO accounts (code, external_reference, name, type, usage, "
                    "parent_code, currency, request_hash) VALUES (:code, :external_reference, "
                    ":name, :type, :usage, :parent_code, 'RWF', :request_hash)"
                ),
                {**account, "request_hash": request_hash({**account, "currency": "RWF"})},
            )
        conn.execute(
            text(
                "UPDATE fault_settings SET mode = NULL, path_prefix = '/api', "
                "delay_seconds = 10, remaining = NULL, retry_after = 5"
            )
        )


@pytest.fixture
def db(engine: Engine) -> Engine:
    reset_database(engine)
    return engine


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, api_key=TEST_API_KEY, timeout_delay=0.2)


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
    return {"X-API-Key": TEST_API_KEY}
