"""/api/accounts against a real Postgres."""

from __future__ import annotations

import threading
import uuid
from typing import Any

from flask.testing import FlaskClient
from flask_openapi3.openapi import OpenAPI
from sqlalchemy import Engine, text

from mock_erp.seed import SEED_ACCOUNTS

URL = "/api/accounts"


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": "1400",
        "external_reference": "GL-1400",
        "name": "Mobile Money",
        "type": "ASSET",
        "usage": "DETAIL",
        "parent_code": "1000",
    }
    body.update(changes)
    return body


def _count(db: Engine, where: str = "true", **params: Any) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(text(f"SELECT count(*) FROM accounts WHERE {where}"), params).scalar_one()
        )


# Create


def test_create_returns_201_with_the_stored_account(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    response = client.post(URL, json=_body(), headers=auth)
    assert response.status_code == 201
    body = response.json
    assert body is not None
    assert body["status"] == "CREATED"
    assert body["code"] == "1400"
    assert body["external_reference"] == "GL-1400"
    assert body["name"] == "Mobile Money"
    assert body["type"] == "ASSET"
    assert body["usage"] == "DETAIL"
    assert body["parent_code"] == "1000"
    assert body["currency"] == "RWF"
    assert body["created_at"]
    assert _count(db, "code = :c", c="1400") == 1


def test_same_request_again_returns_200_already_exists(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    first = client.post(URL, json=_body(), headers=auth)
    again = client.post(URL, json=_body(), headers=auth)
    assert again.status_code == 200
    assert again.json is not None and first.json is not None
    assert again.json["status"] == "ALREADY_EXISTS"
    assert again.json["created_at"] == first.json["created_at"]
    assert _count(db, "code = :c", c="1400") == 1


def test_reposting_a_seed_account_returns_200(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.post(URL, json=dict(SEED_ACCOUNTS[2]), headers=auth)
    assert response.status_code == 200
    assert response.json is not None
    assert response.json["status"] == "ALREADY_EXISTS"


def test_same_reference_different_data_is_a_reference_conflict(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    client.post(URL, json=_body(), headers=auth)
    response = client.post(URL, json=_body(name="Something else"), headers=auth)
    assert response.status_code == 409
    assert response.json is not None
    assert response.json["error"]["code"] == "reference_conflict"
    assert response.json["error"]["details"] == {"external_reference": "GL-1400"}


def test_used_code_with_new_reference_is_code_taken(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    response = client.post(URL, json=_body(code="1100", external_reference="X-1"), headers=auth)
    assert response.status_code == 409
    assert response.json is not None
    assert response.json["error"]["code"] == "code_taken"
    assert response.json["error"]["details"] == {"code": "1100"}
    assert _count(db, "external_reference = 'X-1'") == 0


def _assert_parent_invalid(client: FlaskClient, auth: dict[str, str], body: dict[str, Any]) -> None:
    response = client.post(URL, json=body, headers=auth)
    assert response.status_code == 422
    assert response.json is not None
    assert response.json["error"]["code"] == "parent_invalid"


def test_missing_parent_is_refused(client: FlaskClient, auth: dict[str, str]) -> None:
    _assert_parent_invalid(client, auth, _body(parent_code="9999"))


def test_detail_parent_is_refused(client: FlaskClient, auth: dict[str, str]) -> None:
    _assert_parent_invalid(client, auth, _body(parent_code="1100"))


def test_parent_of_another_type_is_refused(client: FlaskClient, auth: dict[str, str]) -> None:
    _assert_parent_invalid(client, auth, _body(type="EXPENSE", parent_code="1000"))


def test_detail_without_parent_is_refused(client: FlaskClient, auth: dict[str, str]) -> None:
    _assert_parent_invalid(client, auth, _body(parent_code=None))


def test_header_without_parent_is_created(client: FlaskClient, auth: dict[str, str]) -> None:
    body = _body(code="3000", external_reference="GL-3000", name="Equity", type="EQUITY")
    response = client.post(URL, json={**body, "usage": "HEADER", "parent_code": None}, headers=auth)
    assert response.status_code == 201


def test_bad_body_is_invalid_request(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.post(URL, json=_body(currency="USD"), headers=auth)
    assert response.status_code == 422
    assert response.json is not None
    assert response.json["error"]["code"] == "invalid_request"


# List


def test_list_puts_parents_before_children(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get(URL, headers=auth)
    assert response.status_code == 200
    assert response.json is not None
    codes = [a["code"] for a in response.json["items"]]
    assert codes == [
        "1000", "1100", "1200", "1300",
        "2000", "2100",
        "4000", "4100", "4200", "4300", "4400",
        "5000", "5100",
    ]  # fmt: skip
    first = response.json["items"][0]
    assert set(first) == {
        "code",
        "external_reference",
        "name",
        "type",
        "usage",
        "parent_code",
        "currency",
        "created_at",
    }


def test_list_can_filter_by_usage(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get(f"{URL}?usage=HEADER", headers=auth)
    assert response.status_code == 200
    assert response.json is not None
    assert [a["code"] for a in response.json["items"]] == ["1000", "2000", "4000", "5000"]


def test_list_refuses_unknown_usage(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get(f"{URL}?usage=OTHER", headers=auth)
    assert response.status_code == 422


# Balance


def _post_entry(db: Engine, lines: list[tuple[str, int, int]]) -> None:
    """One balanced entry, written with raw SQL in one transaction."""
    entry_id = uuid.uuid4()
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO journal_entries (id, external_reference, request_hash, entry_date, "
                "source) VALUES (:id, :ref, 'h', '2026-10-01', 'MANUAL')"
            ),
            {"id": entry_id, "ref": f"JE-{entry_id}"},
        )
        for account, debit, credit in lines:
            conn.execute(
                text(
                    "INSERT INTO journal_lines (entry_id, account_code, debit, credit) "
                    "VALUES (:e, :a, :d, :c)"
                ),
                {"e": entry_id, "a": account, "d": debit, "c": credit},
            )


def _sample_journal(db: Engine) -> None:
    _post_entry(db, [("1200", 5000, 0), ("1100", 0, 5000)])
    _post_entry(db, [("1100", 1200, 0), ("1200", 0, 1000), ("4100", 0, 200)])


def _balance(client: FlaskClient, auth: dict[str, str], code: str) -> dict[str, Any]:
    response = client.get(f"{URL}/{code}/balance", headers=auth)
    assert response.status_code == 200
    assert response.json is not None
    body: dict[str, Any] = response.json
    return body


def test_detail_balance(client: FlaskClient, auth: dict[str, str], db: Engine) -> None:
    _sample_journal(db)
    assert _balance(client, auth, "1200") == {
        "code": "1200",
        "name": "Loan Receivable",
        "type": "ASSET",
        "usage": "DETAIL",
        "debit_total": 5000,
        "credit_total": 1000,
        "balance": 4000,
        "normal_side": "DEBIT",
    }
    bank = _balance(client, auth, "1100")
    assert (bank["debit_total"], bank["credit_total"], bank["balance"]) == (1200, 5000, -3800)
    interest = _balance(client, auth, "4100")
    assert (interest["balance"], interest["normal_side"]) == (200, "CREDIT")


def test_header_balance_adds_up_its_children(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    _sample_journal(db)
    assets = _balance(client, auth, "1000")
    assert (assets["usage"], assets["debit_total"], assets["credit_total"]) == (
        "HEADER",
        6200,
        6000,
    )
    assert assets["balance"] == 200
    assert _balance(client, auth, "4000")["balance"] == 200
    assert _balance(client, auth, "5000")["balance"] == 0


def test_header_balance_reaches_every_level(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    sub = _body(code="1500", external_reference="GL-1500", name="Other assets", usage="HEADER")
    leaf = _body(code="1510", external_reference="GL-1510", name="Deposits", parent_code="1500")
    assert client.post(URL, json=sub, headers=auth).status_code == 201
    assert client.post(URL, json=leaf, headers=auth).status_code == 201
    _post_entry(db, [("1510", 300, 0), ("4200", 0, 300)])
    assert _balance(client, auth, "1500")["balance"] == 300
    assert _balance(client, auth, "1000")["balance"] == 300


def test_balance_of_unknown_account_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get(f"{URL}/9999/balance", headers=auth)
    assert response.status_code == 404
    assert response.json is not None
    assert response.json["error"]["code"] == "not_found"


# Races and docs


def test_ten_identical_creates_at_once_make_one_row(app: OpenAPI, db: Engine) -> None:
    barrier = threading.Barrier(10)
    statuses: list[int] = []
    lock = threading.Lock()

    def fire() -> None:
        client = app.test_client()
        barrier.wait()
        response = client.post(URL, json=_body(), headers={"X-API-Key": "test-key"})
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=fire) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200] * 9 + [201]
    assert _count(db, "external_reference = 'GL-1400'") == 1


def test_routes_are_in_the_openapi_spec(client: FlaskClient) -> None:
    paths = client.get("/openapi/openapi.json").json["paths"]  # type: ignore[index]
    assert "post" in paths["/api/accounts"]
    assert "get" in paths["/api/accounts"]
    assert "get" in paths["/api/accounts/{code}/balance"]
