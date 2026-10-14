"""/api/journal-entries: post, retry, conflict, rules, reads and races."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask.testing import FlaskClient
from flask_openapi3.openapi import OpenAPI
from sqlalchemy import Engine, text

URL = "/api/journal-entries"


def entry_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "external_reference": "FIN-2026-000002",
        "entry_date": "2026-10-12",
        "source": "REPAYMENT",
        "description": "Repayment LN-2026-000001",
        "reverses": None,
        "lines": [
            {"account_code": "1100", "debit": 458400},
            {"account_code": "1200", "credit": 383400},
            {"account_code": "4100", "credit": 75000},
        ],
    }
    body.update(changes)
    return body


def two_lines(debit_code: str, credit_code: str, amount: int = 1000) -> list[dict[str, Any]]:
    return [
        {"account_code": debit_code, "debit": amount},
        {"account_code": credit_code, "credit": amount},
    ]


def count_rows(engine: Engine, table: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


# POST


def test_post_creates_the_entry(client: FlaskClient, auth: dict[str, str], db: Engine) -> None:
    response = client.post(URL, json=entry_body(), headers={**auth, "X-Correlation-ID": "c-post-1"})
    assert response.status_code == 201
    body = response.json
    assert body["status"] == "CREATED"
    assert body["external_reference"] == "FIN-2026-000002"
    assert body["erp_transaction_id"]
    assert body["entry_date"] == "2026-10-12"
    assert body["source"] == "REPAYMENT"
    assert body["description"] == "Repayment LN-2026-000001"
    assert body["reverses"] is None
    assert body["total"] == 458400
    assert body["correlation_id"] == "c-post-1"
    assert body["created_at"]
    assert body["lines"] == [
        {"account_code": "1100", "account_name": "Bank", "debit": 458400, "credit": 0},
        {"account_code": "1200", "account_name": "Loan Receivable", "debit": 0, "credit": 383400},
        {"account_code": "4100", "account_name": "Interest Income", "debit": 0, "credit": 75000},
    ]
    assert count_rows(db, "journal_lines") == 3


def test_same_post_again_returns_the_stored_entry(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    first = client.post(URL, json=entry_body(), headers=auth)
    reordered = entry_body(lines=list(reversed(entry_body()["lines"])))
    again = client.post(URL, json=reordered, headers=auth)
    assert again.status_code == 200
    assert again.json["status"] == "ALREADY_EXISTS"
    assert again.json["erp_transaction_id"] == first.json["erp_transaction_id"]
    assert again.json["created_at"] == first.json["created_at"]
    assert count_rows(db, "journal_entries") == 1


def test_same_reference_with_different_content_is_a_conflict(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    client.post(URL, json=entry_body(), headers=auth)
    changed = entry_body(lines=two_lines("1100", "1200", 5))
    response = client.post(URL, json=changed, headers=auth)
    assert response.status_code == 409
    assert response.json["error"]["code"] == "reference_conflict"
    assert response.json["error"]["details"] == {"external_reference": "FIN-2026-000002"}
    assert count_rows(db, "journal_entries") == 1


def test_unbalanced_entry_is_refused(client: FlaskClient, auth: dict[str, str], db: Engine) -> None:
    lines = [{"account_code": "1100", "debit": 100}, {"account_code": "1200", "credit": 90}]
    response = client.post(URL, json=entry_body(lines=lines), headers=auth)
    assert response.status_code == 422
    error = response.json["error"]
    assert error["code"] == "entry_not_balanced"
    assert error["message"] == "Debits are 100 and credits are 90."
    assert error["details"] == {"debits": 100, "credits": 90}
    assert count_rows(db, "journal_entries") == 0


def test_unbalanced_retry_of_a_stored_reference_still_fails_422(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    client.post(URL, json=entry_body(), headers=auth)
    lines = [{"account_code": "1100", "debit": 100}, {"account_code": "1200", "credit": 90}]
    response = client.post(URL, json=entry_body(lines=lines), headers=auth)
    assert response.status_code == 422
    assert response.json["error"]["code"] == "entry_not_balanced"


def test_unknown_accounts_are_listed(client: FlaskClient, auth: dict[str, str], db: Engine) -> None:
    lines = [
        {"account_code": "9999", "debit": 10},
        {"account_code": "1100", "debit": 10},
        {"account_code": "8888", "credit": 20},
    ]
    response = client.post(URL, json=entry_body(lines=lines), headers=auth)
    assert response.status_code == 422
    assert response.json["error"]["code"] == "unknown_account"
    assert response.json["error"]["details"] == {"account_codes": ["8888", "9999"]}
    assert count_rows(db, "journal_entries") == 0


def test_header_accounts_are_refused(client: FlaskClient, auth: dict[str, str], db: Engine) -> None:
    response = client.post(URL, json=entry_body(lines=two_lines("1000", "4000")), headers=auth)
    assert response.status_code == 422
    assert response.json["error"]["code"] == "header_account"
    assert response.json["error"]["details"] == {"account_codes": ["1000", "4000"]}
    assert count_rows(db, "journal_entries") == 0


def test_bad_line_is_an_invalid_request(client: FlaskClient, auth: dict[str, str]) -> None:
    lines = [{"account_code": "1100", "debit": 10, "credit": 10}, {"account_code": "1200"}]
    response = client.post(URL, json=entry_body(lines=lines), headers=auth)
    assert response.status_code == 422
    assert response.json["error"]["code"] == "invalid_request"


def test_reversal_of_a_missing_entry_is_an_invalid_request(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    body = entry_body(external_reference="REV-1", source="REVERSAL", reverses="FIN-NOPE")
    response = client.post(URL, json=body, headers=auth)
    assert response.status_code == 422
    assert response.json["error"]["code"] == "invalid_request"
    assert response.json["error"]["details"] == {"reverses": "FIN-NOPE"}
    assert count_rows(db, "journal_entries") == 0


def test_reversal_of_a_stored_entry_is_created(client: FlaskClient, auth: dict[str, str]) -> None:
    client.post(URL, json=entry_body(), headers=auth)
    lines = [
        {"account_code": "1100", "credit": 458400},
        {"account_code": "1200", "debit": 383400},
        {"account_code": "4100", "debit": 75000},
    ]
    body = entry_body(
        external_reference="REV-FIN-2026-000002",
        source="REVERSAL",
        reverses="FIN-2026-000002",
        lines=lines,
    )
    response = client.post(URL, json=body, headers=auth)
    assert response.status_code == 201
    assert response.json["reverses"] == "FIN-2026-000002"


def test_post_without_a_key_is_refused(client: FlaskClient) -> None:
    assert client.post(URL, json=entry_body()).status_code == 401


# GET one


def test_get_returns_the_stored_entry(client: FlaskClient, auth: dict[str, str]) -> None:
    posted = client.post(URL, json=entry_body(), headers=auth).json
    response = client.get(f"{URL}/FIN-2026-000002", headers=auth)
    assert response.status_code == 200
    expected = {k: v for k, v in posted.items() if k != "status"}
    assert {k: v for k, v in response.json.items() if k != "status"} == expected


def test_get_of_a_missing_entry_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get(f"{URL}/FIN-NOPE", headers=auth)
    assert response.status_code == 404
    assert response.json["error"]["code"] == "not_found"
    assert response.json["error"]["details"] == {"external_reference": "FIN-NOPE"}


# Search


def post_simple(
    client: FlaskClient,
    auth: dict[str, str],
    reference: str,
    entry_date: str,
    source: str = "REPAYMENT",
    description: str | None = None,
) -> None:
    body = entry_body(
        external_reference=reference,
        entry_date=entry_date,
        source=source,
        description=description,
        lines=two_lines("1100", "1200"),
    )
    assert client.post(URL, json=body, headers=auth).status_code == 201


def search(client: FlaskClient, auth: dict[str, str], query: str) -> list[str]:
    response = client.get(f"{URL}?{query}", headers=auth)
    assert response.status_code == 200, response.json
    return [item["external_reference"] for item in response.json["items"]]


def seed_three(client: FlaskClient, auth: dict[str, str]) -> None:
    post_simple(client, auth, "FIN-A", "2026-10-01", "DISBURSEMENT", "Loan LN-1 paid out")
    post_simple(client, auth, "FIN-B", "2026-10-05", "REPAYMENT", "Repayment LN-1")
    post_simple(client, auth, "FIN-C", "2026-10-05", "MANUAL", "Bank charge")


def test_search_lists_newest_first(client: FlaskClient, auth: dict[str, str]) -> None:
    seed_three(client, auth)
    response = client.get(URL, headers=auth)
    assert response.status_code == 200
    assert [i["external_reference"] for i in response.json["items"]] == ["FIN-C", "FIN-B", "FIN-A"]
    assert response.json["items"][0]["lines"][0]["account_name"] == "Bank"


def test_search_by_date_range(client: FlaskClient, auth: dict[str, str]) -> None:
    seed_three(client, auth)
    assert search(client, auth, "from=2026-10-02&to=2026-10-05") == ["FIN-C", "FIN-B"]
    assert search(client, auth, "to=2026-10-04") == ["FIN-A"]


def test_search_by_many_references(client: FlaskClient, auth: dict[str, str]) -> None:
    seed_three(client, auth)
    assert search(client, auth, "reference=FIN-A&reference=FIN-C&reference=FIN-X") == [
        "FIN-C",
        "FIN-A",
    ]


def test_search_by_source(client: FlaskClient, auth: dict[str, str]) -> None:
    seed_three(client, auth)
    assert search(client, auth, "source=REPAYMENT") == ["FIN-B"]


def test_search_text_matches_reference_or_description(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    seed_three(client, auth)
    assert search(client, auth, "q=ln-1") == ["FIN-B", "FIN-A"]
    assert search(client, auth, "q=fin-c") == ["FIN-C"]


def test_search_limit_returns_only_the_newest_entries(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    seed_three(client, auth)
    assert search(client, auth, "limit=2") == ["FIN-C", "FIN-B"]


def test_search_limit_out_of_range_is_invalid(client: FlaskClient, auth: dict[str, str]) -> None:
    for limit in ("0", "1001"):
        response = client.get(f"{URL}?limit={limit}", headers=auth)
        assert response.status_code == 422
        assert response.json["error"]["code"] == "invalid_request"


# Races


def post_at_once(app: OpenAPI, auth: dict[str, str], bodies: list[dict[str, Any]]) -> Counter[int]:
    """Send every body from its own thread, released together."""
    start = threading.Barrier(len(bodies))

    def send(body: dict[str, Any]) -> int:
        client = app.test_client()
        start.wait()
        return client.post(URL, json=body, headers=auth).status_code

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        return Counter(pool.map(send, bodies))


def test_ten_identical_posts_at_once_make_one_entry(
    app: OpenAPI, auth: dict[str, str], db: Engine
) -> None:
    statuses = post_at_once(app, auth, [entry_body() for _ in range(10)])
    assert statuses == Counter({201: 1, 200: 9})
    assert count_rows(db, "journal_entries") == 1
    assert count_rows(db, "journal_lines") == 3


def test_ten_different_posts_of_one_reference_make_one_entry(
    app: OpenAPI, auth: dict[str, str], db: Engine
) -> None:
    bodies = [entry_body(lines=two_lines("1100", "1200", 100 + i)) for i in range(10)]
    statuses = post_at_once(app, auth, bodies)
    assert statuses == Counter({201: 1, 409: 9})
    assert count_rows(db, "journal_entries") == 1
    assert count_rows(db, "journal_lines") == 2
