"""The outbox sender's pure rules: how long to wait, and what each ERP answer leads to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from integration_service.clients.errors import (
    ErpConflict,
    ErpError,
    ErpRateLimited,
    ErpRejected,
    ErpServerError,
    ErpTimeout,
    ErpUnavailable,
)
from integration_service.config import Settings, default_gl_roles_path, env_count, env_flag
from integration_service.services.outbox import (
    MAX_BACKOFF_SECONDS,
    Reconcile,
    RetryAt,
    after_failure,
    backoff_seconds,
)

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("attempt", "base"),
    [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 32), (7, 60), (8, 60), (30, 60)],
)
def test_backoff_doubles_up_to_sixty_seconds(attempt: int, base: int) -> None:
    assert backoff_seconds(attempt, 0.0) == base


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5, 6])
def test_jitter_adds_at_most_a_quarter(attempt: int) -> None:
    low = backoff_seconds(attempt, 0.0)
    high = backoff_seconds(attempt, 0.999)
    assert low < high <= low * 1.25


def test_jitter_never_goes_past_the_cap() -> None:
    assert backoff_seconds(12, 0.999) == MAX_BACKOFF_SECONDS


def test_attempt_zero_or_less_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="attempt"):
        backoff_seconds(0, 0.0)


@pytest.mark.parametrize(
    "exc",
    [
        ErpUnavailable("down"),
        ErpTimeout("slow"),
        ErpServerError(500, "boom"),
    ],
)
def test_a_failed_send_waits_and_tries_again(exc: ErpError) -> None:
    decision = after_failure(exc, attempts=3, max_attempts=8, now=NOW, rand=0.0)
    assert isinstance(decision, RetryAt)
    assert decision.at == NOW + timedelta(seconds=4)
    assert decision.counts is True


@pytest.mark.parametrize("exc", [ErpUnavailable("down"), ErpTimeout("slow")])
def test_the_last_attempt_needs_reconciliation(exc: ErpError) -> None:
    decision = after_failure(exc, attempts=8, max_attempts=8, now=NOW, rand=0.0)
    assert isinstance(decision, Reconcile)
    assert "8 attempts" in decision.reason


def test_rate_limit_waits_as_long_as_retry_after_and_does_not_count() -> None:
    decision = after_failure(ErpRateLimited(7), attempts=8, max_attempts=8, now=NOW, rand=0.5)
    assert isinstance(decision, RetryAt)
    assert decision.at == NOW + timedelta(seconds=7)
    assert decision.counts is False


def test_rate_limit_without_retry_after_uses_the_backoff() -> None:
    decision = after_failure(ErpRateLimited(None), attempts=2, max_attempts=8, now=NOW, rand=0.0)
    assert isinstance(decision, RetryAt)
    assert decision.at == NOW + timedelta(seconds=2)


def test_conflict_needs_reconciliation_at_once() -> None:
    conflict = ErpConflict(409, {"error": {"code": "reference_conflict", "message": "x"}})
    decision = after_failure(conflict, attempts=1, max_attempts=8, now=NOW, rand=0.0)
    assert isinstance(decision, Reconcile)
    assert "different content" in decision.reason


def test_a_refused_entry_needs_reconciliation_at_once() -> None:
    refused = ErpRejected(422, {"error": {"code": "entry_not_balanced", "message": "x"}})
    decision = after_failure(refused, attempts=1, max_attempts=8, now=NOW, rand=0.0)
    assert isinstance(decision, Reconcile)
    assert "entry_not_balanced" in decision.reason


@pytest.mark.parametrize("status", [401, 403])
def test_a_wrong_erp_key_is_retried_not_reconciled(status: int) -> None:
    refused = ErpRejected(status, {"error": {"code": "unauthorized", "message": "x"}})
    decision = after_failure(refused, attempts=1, max_attempts=8, now=NOW, rand=0.0)
    assert isinstance(decision, RetryAt)


# --- settings for the loop ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "value"),
    [("", True), ("true", True), ("1", True), ("ON", True), ("false", False), ("0", False)],
)
def test_outbox_enabled_flag(raw: str, value: bool) -> None:
    assert env_flag({"OUTBOX_ENABLED": raw}, "OUTBOX_ENABLED", True) is value


def test_a_bad_flag_is_refused() -> None:
    with pytest.raises(RuntimeError, match="OUTBOX_ENABLED"):
        env_flag({"OUTBOX_ENABLED": "maybe"}, "OUTBOX_ENABLED", True)


@pytest.mark.parametrize(("raw", "value"), [("", 8), ("3", 3)])
def test_max_attempts_setting(raw: str, value: int) -> None:
    assert env_count({"OUTBOX_MAX_ATTEMPTS": raw}, "OUTBOX_MAX_ATTEMPTS", 8) == value


@pytest.mark.parametrize("raw", ["0", "-1", "two", "1.5"])
def test_a_bad_count_is_refused(raw: str) -> None:
    with pytest.raises(RuntimeError, match="OUTBOX_MAX_ATTEMPTS"):
        env_count({"OUTBOX_MAX_ATTEMPTS": raw}, "OUTBOX_MAX_ATTEMPTS", 8)


def test_settings_built_in_code_start_no_loop() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://x",
        partner_api_keys={"a": "k"},
        fineract_base_url="http://f",
        fineract_username="u",
        fineract_password="p",
        erp_base_url="http://e",
        erp_api_key="k",
    )
    assert settings.outbox_enabled is False
    assert settings.gl_roles_config_path.endswith("loan-product.json")


def test_from_env_reads_the_loop_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "INTEGRATION_DATABASE_URL": "postgresql+psycopg://x",
        "PARTNER_API_KEYS": "a:k",
        "FINERACT_BASE_URL": "http://f",
        "FINERACT_USERNAME": "u",
        "FINERACT_PASSWORD": "p",
        "ERP_BASE_URL": "http://e",
        "ERP_API_KEY": "k",
        "OUTBOX_POLL_SECONDS": "2.5",
        "OUTBOX_MAX_ATTEMPTS": "5",
        "GL_ROLES_CONFIG_PATH": "/conf/loan-product.json",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OUTBOX_ENABLED", raising=False)
    settings = Settings.from_env()
    assert settings.outbox_enabled is True
    assert settings.outbox_poll_seconds == 2.5
    assert settings.outbox_max_attempts == 5
    assert settings.gl_roles_config_path == "/conf/loan-product.json"


def test_the_setup_config_dir_gives_the_default_product_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINERACT_SETUP_CONFIG_DIR", "/app/fineract-configuration")
    assert str(default_gl_roles_path()) == "/app/fineract-configuration/loan-product.json"
    monkeypatch.delenv("FINERACT_SETUP_CONFIG_DIR")
    assert default_gl_roles_path().is_file()
