"""Fault rules that need no database: which paths, which defaults, which status."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mock_erp.errors import ErrorCode
from mock_erp.faults import FaultDecision, default_delay, fault_error, path_can_fault
from mock_erp.schemas.faults import FaultMode, FaultSettingIn


@pytest.mark.parametrize(
    ("path", "prefix", "expected"),
    [
        ("/api/accounts", "/api", True),
        ("/api/journal-entries", "/api/journal", True),
        ("/api/accounts", "/api/journal", False),
        ("/apiary", "/api", True),
        ("/health", "/api", False),
        ("/admin/faults", "/api", False),
        ("/ui/faults", "/api", False),
        ("/openapi/swagger", "/api", False),
        ("/health", "/", False),
        ("/admin/faults", "/", False),
    ],
)
def test_path_can_fault(path: str, prefix: str, expected: bool) -> None:
    assert path_can_fault(path, prefix) is expected


def test_put_model_defaults() -> None:
    body = FaultSettingIn(mode="down")
    assert body.path_prefix == "/api"
    assert body.delay_seconds is None
    assert body.remaining is None
    assert body.retry_after == 5


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "broken"},
        {"mode": None},
        {},
        {"mode": "down", "path_prefix": "/admin"},
        {"mode": "down", "path_prefix": "/health"},
        {"mode": "down", "path_prefix": "api"},
        {"mode": "timeout", "delay_seconds": 0},
        {"mode": "timeout", "delay_seconds": 121},
        {"mode": "down", "remaining": 0},
        {"mode": "rate_limited", "retry_after": 0},
        {"mode": "rate_limited", "retry_after": 3601},
        {"mode": "down", "surprise": 1},
    ],
)
def test_put_model_rejects_bad_values(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FaultSettingIn.model_validate(bad)


def test_put_model_accepts_full_body() -> None:
    body = FaultSettingIn.model_validate(
        {
            "mode": "timeout_after_write",
            "path_prefix": "/api/journal-entries",
            "delay_seconds": 120,
            "remaining": 1,
            "retry_after": 3600,
        }
    )
    assert body.mode == "timeout_after_write"
    assert body.delay_seconds == 120


@pytest.mark.parametrize(
    ("configured", "expected"), [(0.2, 1), (1.0, 1), (9.1, 10), (10.0, 10), (500.0, 120)]
)
def test_default_delay_rounds_up_into_range(configured: float, expected: int) -> None:
    assert default_delay(configured) == expected


@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [
        ("down", 503, ErrorCode.ERP_UNAVAILABLE),
        ("rate_limited", 429, ErrorCode.RATE_LIMITED),
        ("timeout", 504, ErrorCode.TIMEOUT),
    ],
)
def test_decision_maps_mode_to_error(mode: FaultMode, status: int, code: ErrorCode) -> None:
    err = fault_error(FaultDecision(mode=mode, delay_seconds=3, retry_after=7))
    assert err is not None
    assert err.status == status
    assert err.code == code


def test_rate_limited_error_carries_retry_after() -> None:
    err = fault_error(FaultDecision(mode="rate_limited", delay_seconds=3, retry_after=7))
    assert err is not None
    assert err.headers == {"Retry-After": "7"}


def test_timeout_after_write_lets_the_route_run() -> None:
    decision = FaultDecision(mode="timeout_after_write", delay_seconds=3, retry_after=7)
    assert fault_error(decision) is None
