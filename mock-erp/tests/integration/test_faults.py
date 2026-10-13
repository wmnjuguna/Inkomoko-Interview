"""The fault switch end to end: the service functions, /admin/faults and the request hooks."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from flask.testing import FlaskClient
from flask_openapi3.openapi import OpenAPI
from sqlalchemy import Engine, text

from mock_erp import faults
from mock_erp.faults import FaultDecision, clear_fault, get_setting, set_fault, take_fault

# --- service functions -------------------------------------------------------------------


def test_no_fault_by_default(db: Engine) -> None:
    setting = get_setting(db)
    assert setting.mode is None
    assert setting.active is False
    assert take_fault(db, "/api/accounts") is None


def test_set_then_get_then_clear(db: Engine) -> None:
    saved = set_fault(db, "rate_limited", path_prefix="/api/journal", remaining=3, retry_after=9)
    assert saved.mode == "rate_limited"
    assert saved.path_prefix == "/api/journal"
    assert saved.remaining == 3
    assert saved.retry_after == 9
    assert saved.active is True
    assert get_setting(db) == saved

    cleared = clear_fault(db)
    assert cleared.mode is None
    assert cleared.active is False
    assert cleared.remaining is None


def test_take_fault_returns_the_decision(db: Engine) -> None:
    set_fault(db, "timeout", delay_seconds=7, retry_after=4)
    assert take_fault(db, "/api/accounts") == FaultDecision("timeout", 7, 4)
    assert get_setting(db).mode == "timeout"


def test_take_fault_only_on_matching_paths(db: Engine) -> None:
    set_fault(db, "down", path_prefix="/api/journal", remaining=1)
    assert take_fault(db, "/api/accounts") is None
    assert take_fault(db, "/health") is None
    assert get_setting(db).remaining == 1


def test_remaining_counts_down_then_switches_off(db: Engine) -> None:
    set_fault(db, "down", remaining=2)
    assert take_fault(db, "/api/accounts") == FaultDecision("down", 10, 5)
    assert get_setting(db).remaining == 1
    assert take_fault(db, "/api/accounts") == FaultDecision("down", 10, 5)
    after = get_setting(db)
    assert after.mode is None
    assert after.remaining == 0
    assert after.active is False
    assert take_fault(db, "/api/accounts") is None


def test_take_fault_is_exact_under_concurrency(db: Engine) -> None:
    set_fault(db, "down", remaining=5)
    start = Barrier(20)

    def call(_: int) -> FaultDecision | None:
        start.wait()
        return take_fault(db, "/api/accounts")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(call, range(20)))

    assert sum(1 for r in results if r is not None) == 5
    assert get_setting(db).mode is None


# --- /admin/faults -----------------------------------------------------------------------


def test_admin_get_shows_no_fault(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get("/admin/faults", headers=auth)
    assert response.status_code == 200
    assert response.json == {
        "mode": None,
        "path_prefix": "/api",
        "delay_seconds": 10,
        "remaining": None,
        "retry_after": 5,
        "active": False,
    }


def test_admin_put_uses_defaults(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.put("/admin/faults", headers=auth, json={"mode": "timeout"})
    assert response.status_code == 200
    # ERP_TIMEOUT_DELAY is 0.2 in tests, rounded up to whole seconds.
    assert response.json == {
        "mode": "timeout",
        "path_prefix": "/api",
        "delay_seconds": 1,
        "remaining": None,
        "retry_after": 5,
        "active": True,
    }
    assert client.get("/admin/faults", headers=auth).json == response.json


def test_admin_put_full_body(client: FlaskClient, auth: dict[str, str]) -> None:
    body = {
        "mode": "rate_limited",
        "path_prefix": "/api/journal-entries",
        "delay_seconds": 30,
        "remaining": 2,
        "retry_after": 60,
    }
    response = client.put("/admin/faults", headers=auth, json=body)
    assert response.status_code == 200
    assert response.json == {**body, "active": True}


def test_admin_put_rejects_bad_body(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.put(
        "/admin/faults", headers=auth, json={"mode": "down", "path_prefix": "/admin"}
    )
    assert response.status_code == 422
    assert response.json is not None
    assert response.json["error"]["code"] == "invalid_request"
    assert get_setting_mode(client, auth) is None


def test_admin_delete_clears(client: FlaskClient, auth: dict[str, str]) -> None:
    client.put("/admin/faults", headers=auth, json={"mode": "down", "remaining": 3})
    response = client.delete("/admin/faults", headers=auth)
    assert response.status_code == 200
    assert response.json is not None
    assert response.json["mode"] is None
    assert response.json["active"] is False
    assert get_setting_mode(client, auth) is None


def test_admin_needs_the_api_key(client: FlaskClient) -> None:
    assert client.get("/admin/faults").status_code == 401
    assert client.put("/admin/faults", json={"mode": "down"}).status_code == 401
    assert client.delete("/admin/faults").status_code == 401


def get_setting_mode(client: FlaskClient, auth: dict[str, str]) -> str | None:
    body = client.get("/admin/faults", headers=auth).json
    assert body is not None
    mode: str | None = body["mode"]
    return mode


# --- the request hooks -------------------------------------------------------------------
# A test-only route under /api that really writes to the database, so we can see whether
# the route ran and whether its write was saved.


@pytest.fixture
def probe(app: OpenAPI, db: Engine) -> Engine:
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS fault_probe (id serial PRIMARY KEY)"))
        conn.execute(text("TRUNCATE fault_probe"))

    def write() -> tuple[dict[str, Any], int]:
        with db.begin() as conn:
            conn.execute(text("INSERT INTO fault_probe DEFAULT VALUES"))
        return {"saved": True}, 201

    def other() -> tuple[dict[str, Any], int]:
        return {"ok": True}, 200

    app.add_url_rule("/api/_probe", "probe_write", write, methods=["POST"])
    app.add_url_rule("/api/_other", "probe_other", other)
    return db


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the fault delays instead of waiting."""
    calls: list[float] = []
    monkeypatch.setattr(faults, "sleep", calls.append)
    return calls


def saved_rows(engine: Engine) -> int:
    with engine.connect() as conn:
        count: int = conn.execute(text("SELECT count(*) FROM fault_probe")).scalar_one()
    return count


def test_no_fault_lets_the_route_run(
    client: FlaskClient, auth: dict[str, str], probe: Engine, sleeps: list[float]
) -> None:
    assert client.post("/api/_probe", headers=auth).status_code == 201
    assert saved_rows(probe) == 1
    assert sleeps == []


def test_down_returns_503_and_saves_nothing(
    client: FlaskClient, auth: dict[str, str], probe: Engine
) -> None:
    set_fault(probe, "down")
    response = client.post("/api/_probe", headers=auth)
    assert response.status_code == 503
    assert response.json is not None
    assert response.json["error"]["code"] == "erp_unavailable"
    assert saved_rows(probe) == 0


def test_rate_limited_returns_429_with_retry_after(
    client: FlaskClient, auth: dict[str, str], probe: Engine
) -> None:
    set_fault(probe, "rate_limited", retry_after=42)
    response = client.post("/api/_probe", headers=auth)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert response.json is not None
    assert response.json["error"]["code"] == "rate_limited"
    assert saved_rows(probe) == 0


def test_timeout_waits_then_504_and_route_never_runs(
    client: FlaskClient, auth: dict[str, str], probe: Engine, sleeps: list[float]
) -> None:
    set_fault(probe, "timeout", delay_seconds=3)
    response = client.post("/api/_probe", headers=auth)
    assert response.status_code == 504
    assert response.json is not None
    assert response.json["error"]["code"] == "timeout"
    assert sleeps == [3]
    assert saved_rows(probe) == 0


def test_timeout_after_write_saves_then_waits_then_answers(
    client: FlaskClient, auth: dict[str, str], probe: Engine, sleeps: list[float]
) -> None:
    set_fault(probe, "timeout_after_write", delay_seconds=4)
    response = client.post("/api/_probe", headers=auth)
    assert response.status_code == 201
    assert response.json == {"saved": True}
    assert sleeps == [4]
    assert saved_rows(probe) == 1


def test_timeout_after_write_really_waits(
    client: FlaskClient, auth: dict[str, str], probe: Engine
) -> None:
    set_fault(probe, "timeout_after_write", delay_seconds=1)
    started = time.perf_counter()
    response = client.post("/api/_probe", headers=auth)
    assert response.status_code == 201
    assert time.perf_counter() - started >= 1.0
    assert saved_rows(probe) == 1


def test_remaining_two_faults_exactly_two_calls(
    client: FlaskClient, auth: dict[str, str], probe: Engine
) -> None:
    set_fault(probe, "down", remaining=2)
    statuses = [client.post("/api/_probe", headers=auth).status_code for _ in range(4)]
    assert statuses == [503, 503, 201, 201]
    assert saved_rows(probe) == 2
    assert get_setting(probe).active is False


def test_path_prefix_targets_only_matching_paths(
    client: FlaskClient, auth: dict[str, str], probe: Engine
) -> None:
    set_fault(probe, "down", path_prefix="/api/_probe")
    assert client.get("/api/_other", headers=auth).status_code == 200
    assert client.post("/api/_probe", headers=auth).status_code == 503


def test_health_and_admin_never_faulted(
    client: FlaskClient, auth: dict[str, str], probe: Engine
) -> None:
    # The API refuses a prefix outside /api; force one into the row to prove the guard.
    with probe.begin() as conn:
        conn.execute(text("UPDATE fault_settings SET mode = 'down', path_prefix = '/'"))
    assert client.get("/health").status_code == 200
    assert client.get("/admin/faults", headers=auth).status_code == 200
    assert client.get("/openapi/openapi.json").status_code == 200
    assert client.get("/api/_other", headers=auth).status_code == 503


def test_call_without_key_does_not_use_up_a_fault(
    client: FlaskClient, auth: dict[str, str], probe: Engine
) -> None:
    set_fault(probe, "down", remaining=1)
    assert client.post("/api/_probe").status_code == 401
    assert get_setting(probe).remaining == 1
