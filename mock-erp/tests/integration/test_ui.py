"""Smoke tests for the server-rendered pages under /ui (Jinja2 + HTMX)."""

from __future__ import annotations

import json
import uuid

import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine, text

HX = {"HX-Request": "true"}

NEW_ACCOUNT = {
    "code": "1400",
    "external_reference": "GL-1400",
    "name": "Micro Loans",
    "type": "ASSET",
    "usage": "DETAIL",
    "parent_code": "1000",
}


def post_entry(engine: Engine, ref: str, amount: int) -> None:
    """One balanced entry written straight to the tables: Bank debit, Loan credit."""
    entry_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO journal_entries (id, external_reference, request_hash, entry_date, "
                "source, description) VALUES (:id, :ref, 'ui-test', DATE '2026-10-12', "
                "'DISBURSEMENT', 'UI test entry')"
            ),
            {"id": entry_id, "ref": ref},
        )
        for code, debit, credit in (("1200", amount, 0), ("1100", 0, amount)):
            conn.execute(
                text(
                    "INSERT INTO journal_lines (entry_id, account_code, debit, credit) "
                    "VALUES (:id, :code, :debit, :credit)"
                ),
                {"id": entry_id, "code": code, "debit": debit, "credit": credit},
            )


def toast_of(response: object) -> dict[str, str]:
    trigger = response.headers.get("HX-Trigger", "")  # type: ignore[attr-defined]
    assert trigger, "expected an HX-Trigger header carrying a toast"
    toast: dict[str, str] = json.loads(trigger)["toast"]
    return toast


# Layout and pages


def test_root_redirects_to_the_chart(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code in (301, 302, 303, 308)
    assert response.headers["Location"].endswith("/ui/accounts")


def test_accounts_page_shows_the_seeded_chart(client: FlaskClient) -> None:
    response = client.get("/ui/accounts")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "<html" in page
    assert "Chart of accounts" in page
    assert "Loan Receivable" in page
    assert "GL-1200" in page
    assert "Mock ERP" in page
    # The layout: sidebar links, health pill, activity rail, toast region.
    for href in ("/ui/accounts", "/ui/journal", "/ui/journal/new", "/ui/faults"):
        assert f'href="{href}"' in page
    assert "ERP healthy" in page
    assert 'aria-label="Live activity"' in page
    assert 'aria-live="polite"' in page
    assert "/ui/static/htmx.min.js" in page
    assert "/ui/static/app.css" in page


def test_accounts_page_shows_kpis_and_balances(client: FlaskClient, db: Engine) -> None:
    post_entry(db, "DISB-UI-1", 250_000)
    page = client.get("/ui/accounts").get_data(as_text=True)
    assert "Journal entries" in page
    assert "Ledger total" in page
    assert "250,000" in page


def test_accounts_table_partial_refreshes_without_the_layout(client: FlaskClient) -> None:
    response = client.get("/ui/accounts/table", headers=HX)
    assert response.status_code == 200
    fragment = response.get_data(as_text=True)
    assert "Loan Receivable" in fragment
    assert "<html" not in fragment


def test_activity_partial_lists_latest_entries(client: FlaskClient, db: Engine) -> None:
    post_entry(db, "DISB-UI-2", 120_000)
    response = client.get("/ui/activity", headers=HX)
    assert response.status_code == 200
    fragment = response.get_data(as_text=True)
    assert "DISB-UI-2" in fragment
    assert "120,000" in fragment
    assert "<html" not in fragment


def test_activity_partial_keeps_only_the_latest_eight(client: FlaskClient, db: Engine) -> None:
    for i in range(10):
        post_entry(db, f"REF-{i:02d}", 1000 + i)
    fragment = client.get("/ui/activity", headers=HX).get_data(as_text=True)
    assert fragment.count('class="activity-item') == 8


def test_static_assets_are_served(client: FlaskClient) -> None:
    for path in (
        "/ui/static/app.css",
        "/ui/static/htmx.min.js",
        "/ui/static/fonts/bricolage-grotesque-latin-wght-normal.woff2",
        "/ui/static/fonts/plus-jakarta-sans-latin-wght-normal.woff2",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        response.close()


# Add account


def test_add_account_creates_it_and_refreshes_the_tree(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    response = client.post("/ui/accounts", data=NEW_ACCOUNT, headers=HX)
    assert response.status_code == 200
    fragment = response.get_data(as_text=True)
    assert "Micro Loans" in fragment
    assert 'hx-swap-oob="true"' in fragment
    assert toast_of(response)["kind"] == "good"
    assert "1400" in toast_of(response)["message"]

    listed = client.get("/api/accounts", headers=auth).get_json()
    assert "1400" in [a["code"] for a in listed["items"]]
    assert "Micro Loans" in client.get("/ui/accounts").get_data(as_text=True)


def test_add_account_with_a_bad_parent_shows_the_api_message(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    response = client.post("/ui/accounts", data={**NEW_ACCOUNT, "parent_code": "9999"}, headers=HX)
    assert response.status_code == 422
    fragment = response.get_data(as_text=True)
    assert "Parent account 9999 does not exist." in fragment
    # The form keeps what was typed.
    assert 'value="Micro Loans"' in fragment
    listed = client.get("/api/accounts", headers=auth).get_json()
    assert "1400" not in [a["code"] for a in listed["items"]]


def test_add_account_with_a_bad_code_shows_a_validation_message(client: FlaskClient) -> None:
    response = client.post("/ui/accounts", data={**NEW_ACCOUNT, "code": "14"}, headers=HX)
    assert response.status_code == 422
    assert "code:" in response.get_data(as_text=True)


def test_add_header_account_without_parent(client: FlaskClient) -> None:
    data = {**NEW_ACCOUNT, "code": "3000", "external_reference": "GL-3000", "name": "Equity"}
    data.update(type="EQUITY", usage="HEADER", parent_code="")
    response = client.post("/ui/accounts", data=data, headers=HX)
    assert response.status_code == 200
    assert "Equity" in response.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/ui/accounts", "/ui/faults", "/ui/faults/clear"])
def test_posts_without_the_htmx_header_are_refused(client: FlaskClient, path: str) -> None:
    response = client.post(path, data={**NEW_ACCOUNT, "mode": "down"})
    assert response.status_code == 400


# Fault switches


def test_faults_page_shows_every_mode_and_what_callers_see(client: FlaskClient) -> None:
    response = client.get("/ui/faults")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Fault switches" in page
    for answer in (
        "503 erp_unavailable",
        "504 timeout",
        "entry saved",
        "429 rate_limited",
        "Retry-After",
    ):
        assert answer in page
    for mode in ("down", "timeout", "timeout_after_write", "rate_limited"):
        assert f'value="{mode}"' in page


def test_fault_toggle_breaks_the_api_and_clear_restores_it(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    assert client.get("/api/accounts", headers=auth).status_code == 200

    response = client.post("/ui/faults", data={"mode": "down", "path_prefix": "/api"}, headers=HX)
    assert response.status_code == 200
    assert toast_of(response)["kind"] == "warn"
    assert client.get("/api/accounts", headers=auth).status_code == 503

    # Every page shows the active fault and links to the switches.
    page = client.get("/ui/accounts").get_data(as_text=True)
    assert "Fault: down" in page

    response = client.post("/ui/faults/clear", headers=HX)
    assert response.status_code == 200
    assert toast_of(response)["kind"] == "good"
    assert client.get("/api/accounts", headers=auth).status_code == 200


def test_fault_options_are_passed_through(client: FlaskClient, auth: dict[str, str]) -> None:
    form = {
        "mode": "rate_limited",
        "path_prefix": "/api/accounts",
        "remaining": "1",
        "retry_after": "7",
        "delay_seconds": "3",
    }
    assert client.post("/ui/faults", data=form, headers=HX).status_code == 200
    page = client.get("/ui/faults").get_data(as_text=True)
    assert "Retry-After: 7" in page

    limited = client.get("/api/accounts", headers=auth)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "7"
    # Only one call was asked for.
    assert client.get("/api/accounts", headers=auth).status_code == 200


def test_fault_with_bad_options_shows_the_problem(client: FlaskClient) -> None:
    response = client.post("/ui/faults", data={"mode": "down", "path_prefix": "/x"}, headers=HX)
    assert response.status_code == 422
    assert "must start with /api" in response.get_data(as_text=True)


def test_ui_is_never_faulted(client: FlaskClient) -> None:
    client.post("/ui/faults", data={"mode": "down"}, headers=HX)
    for path in ("/ui/accounts", "/ui/accounts/table", "/ui/activity", "/ui/faults"):
        assert client.get(path, headers=HX).status_code == 200, path
    assert client.post("/ui/faults/clear", headers=HX).status_code == 200
