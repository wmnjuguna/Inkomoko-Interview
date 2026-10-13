"""Fault switch: lets the ERP fail on purpose (down, timeout, timeout after write, 429).

The setting is one database row, so every Gunicorn worker sees the same switch.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flask import Flask, Response, g, request
from sqlalchemy import Engine, RowMapping, func, select, text, update

from mock_erp.db import get_engine
from mock_erp.errors import ErpError, ErrorCode
from mock_erp.logs import log_field
from mock_erp.models import fault_settings
from mock_erp.schemas.faults import FaultMode, FaultSettingOut

# How the timeout modes wait. Tests swap it for a recorder so they stay fast.
sleep: Callable[[float], None] = time.sleep

# These paths never fail on purpose: you must always be able to check health and turn it off.
NEVER_FAULTED = ("/health", "/admin", "/ui", "/openapi")


def path_can_fault(path: str, path_prefix: str) -> bool:
    """True when a fault set for path_prefix applies to this request path."""
    return not path.startswith(NEVER_FAULTED) and path.startswith(path_prefix)


def default_delay(timeout_delay: float) -> int:
    """The ERP_TIMEOUT_DELAY setting as whole seconds, rounded up, kept within 1-120."""
    return min(120, max(1, math.ceil(timeout_delay)))


@dataclass(frozen=True)
class FaultDecision:
    """This request fails on purpose, in this way."""

    mode: FaultMode
    delay_seconds: int
    retry_after: int


def fault_error(decision: FaultDecision) -> ErpError | None:
    """The error to send instead of running the route. None means: run the route."""
    if decision.mode == "down":
        return ErpError(ErrorCode.ERP_UNAVAILABLE, "The ERP is down (fault switch).", 503)
    if decision.mode == "rate_limited":
        return ErpError(
            ErrorCode.RATE_LIMITED,
            "Too many requests (fault switch).",
            429,
            headers={"Retry-After": str(decision.retry_after)},
        )
    if decision.mode == "timeout":
        return ErpError(ErrorCode.TIMEOUT, "The ERP timed out (fault switch).", 504)
    return None


# One statement, so a limited fault is exact across Gunicorn workers: the row lock makes
# concurrent calls queue up, and each one sees the count the previous one left.
# The last use switches the fault off. RETURNING old.* (Postgres 18) gives the values
# from before this update, so the last call still gets its mode.
_TAKE_FAULT = text(
    """
    UPDATE fault_settings
       SET remaining = CASE WHEN remaining IS NULL THEN NULL ELSE remaining - 1 END,
           mode = CASE WHEN remaining = 1 THEN NULL ELSE mode END,
           updated_at = CASE WHEN remaining = 1 THEN now() ELSE updated_at END
     WHERE id = 1
       AND mode IS NOT NULL
       AND (remaining IS NULL OR remaining > 0)
       AND starts_with(:path, path_prefix)
    RETURNING old.mode, old.delay_seconds, old.retry_after
    """
)


def _to_setting(row: RowMapping) -> FaultSettingOut:
    return FaultSettingOut(**row, active=row["mode"] is not None)


_COLUMNS = (
    fault_settings.c.mode,
    fault_settings.c.path_prefix,
    fault_settings.c.delay_seconds,
    fault_settings.c.remaining,
    fault_settings.c.retry_after,
)


def get_setting(engine: Engine) -> FaultSettingOut:
    """The current fault setting."""
    with engine.connect() as conn:
        row = conn.execute(select(*_COLUMNS).where(fault_settings.c.id == 1)).one()
    return _to_setting(row._mapping)


def set_fault(
    engine: Engine,
    mode: FaultMode,
    *,
    path_prefix: str = "/api",
    delay_seconds: int = 10,
    remaining: int | None = None,
    retry_after: int = 5,
) -> FaultSettingOut:
    """Turn a fault on, replacing any earlier one. remaining None means until cleared."""
    return _update(
        engine,
        mode=mode,
        path_prefix=path_prefix,
        delay_seconds=delay_seconds,
        remaining=remaining,
        retry_after=retry_after,
    )


def clear_fault(engine: Engine) -> FaultSettingOut:
    """Turn the fault off."""
    return _update(engine, mode=None, remaining=None)


def _update(engine: Engine, **values: Any) -> FaultSettingOut:
    statement = (
        update(fault_settings)
        .where(fault_settings.c.id == 1)
        .values(**values, updated_at=func.now())
        .returning(*_COLUMNS)
    )
    with engine.begin() as conn:
        row = conn.execute(statement).one()
    return _to_setting(row._mapping)


def take_fault(engine: Engine, path: str) -> FaultDecision | None:
    """Use up one fault for this path, if one applies. None means: handle normally."""
    if path.startswith(NEVER_FAULTED):
        return None
    with engine.begin() as conn:
        row = conn.execute(_TAKE_FAULT, {"path": path}).one_or_none()
    if row is None:
        return None
    return FaultDecision(
        mode=row.mode, delay_seconds=row.delay_seconds, retry_after=row.retry_after
    )


def register_fault_hooks(app: Flask) -> None:
    """Fail requests on purpose while a fault is on. Runs after the API key check."""

    @app.before_request
    def apply_fault() -> None:
        decision = take_fault(get_engine(), request.path)
        if decision is None:
            return
        g.fault = decision
        log_field("fault", decision.mode)
        if decision.mode == "timeout":
            sleep(decision.delay_seconds)
        err = fault_error(decision)
        if err is not None:
            raise err

    @app.after_request
    def delay_after_write(response: Response) -> Response:
        # The route has run and committed. The caller has likely given up by the time
        # this answer arrives, but the write is saved.
        decision: FaultDecision | None = g.get("fault")
        if decision is not None and decision.mode == "timeout_after_write":
            sleep(decision.delay_seconds)
        return response
