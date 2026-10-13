"""Settings.from_env: blank and bad number values."""

from __future__ import annotations

import pytest

from integration_service.config import Settings

BASE_ENV = {
    "INTEGRATION_DATABASE_URL": "postgresql+psycopg://u:p@localhost:1/x",
    "PARTNER_API_KEYS": "a:k",
    "FINERACT_BASE_URL": "http://f/fineract-provider/api/v1",
    "FINERACT_USERNAME": "u",
    "FINERACT_PASSWORD": "p",
    "ERP_BASE_URL": "http://e",
    "ERP_API_KEY": "k",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_blank_timeouts_use_the_defaults(env: pytest.MonkeyPatch) -> None:
    # Compose passes ${INTEGRATION_FINERACT_TIMEOUT_SECONDS} as "" when .env lacks it.
    env.setenv("FINERACT_TIMEOUT_SECONDS", "")
    env.setenv("ERP_TIMEOUT_SECONDS", "  ")

    settings = Settings.from_env()

    assert settings.fineract_timeout_seconds == 30.0
    assert settings.erp_timeout_seconds == 10.0


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_a_bad_timeout_names_the_variable(env: pytest.MonkeyPatch, value: str) -> None:
    env.setenv("FINERACT_TIMEOUT_SECONDS", value)

    with pytest.raises(RuntimeError, match="FINERACT_TIMEOUT_SECONDS must be a number of seconds"):
        Settings.from_env()
