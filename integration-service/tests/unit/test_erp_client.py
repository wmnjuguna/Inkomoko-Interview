"""The ERP client, against a fake transport: headers, parsing and error mapping."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest

from integration_service.clients.erp import ErpAccount, ErpClient
from integration_service.clients.errors import (
    ErpConflict,
    ErpRateLimited,
    ErpRejected,
    ErpServerError,
    ErpTimeout,
    ErpUnavailable,
)

BASE = "http://erp.test"
Handler = Callable[[httpx.Request], httpx.Response]

ACCOUNT = {
    "code": "1200",
    "external_reference": "GL-1200",
    "name": "Loan Receivable",
    "type": "ASSET",
    "usage": "DETAIL",
    "parent_code": "1000",
    "currency": "RWF",
    "created_at": "2026-10-12T10:00:00Z",
}
ENTRY = {
    "external_reference": "DSB:abc",
    "entry_date": "2026-10-12",
    "source": "DISBURSEMENT",
    "lines": [
        {"account_code": "1200", "debit": 1000},
        {"account_code": "1100", "credit": 1000},
    ],
}


def make_client(handler: Handler) -> ErpClient:
    return ErpClient(
        base_url=BASE,
        api_key="erp-key",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def recorder(
    status: int = 200, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[list[httpx.Request], ErpClient]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {}, headers=headers)

    return seen, make_client(handler)


def erp_error(code: str, message: str = "nope") -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": {"field": "x"},
            "correlation_id": "c-1",
        }
    }


def raising(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# --- accounts ------------------------------------------------------------------------------


def test_list_accounts_sends_key_and_parses_items() -> None:
    header: dict[str, Any] = {
        **ACCOUNT,
        "code": "1000",
        "external_reference": "GL-1000",
        "usage": "HEADER",
        "parent_code": None,
    }
    seen, client = recorder(body={"items": [header, ACCOUNT]})
    accounts = client.list_accounts()
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/api/accounts"
    assert request.headers["X-API-Key"] == "erp-key"
    assert request.headers["Accept"] == "application/json"
    assert [a.code for a in accounts] == ["1000", "1200"]
    assert isinstance(accounts[1], ErpAccount)
    assert accounts[1].external_reference == "GL-1200"
    assert accounts[1].type == "ASSET"
    assert accounts[1].usage == "DETAIL"
    assert accounts[1].parent_code == "1000"
    assert accounts[0].parent_code is None


def test_list_accounts_with_bad_shape_is_server_error() -> None:
    _, client = recorder(body={"items": [{"code": "1"}]})
    with pytest.raises(ErpServerError):
        client.list_accounts()


def test_correlation_id_is_passed_through_when_given() -> None:
    seen, client = recorder(body={"items": []})
    client.list_accounts(correlation_id="c-xyz")
    assert seen[0].headers["X-Correlation-ID"] == "c-xyz"


def test_no_correlation_header_when_none_given() -> None:
    seen, client = recorder(body={"items": []})
    client.list_accounts()
    assert "X-Correlation-ID" not in seen[0].headers


# --- journal -------------------------------------------------------------------------------


def test_post_journal_entry_sends_json_and_correlation_id() -> None:
    answer = {**ENTRY, "status": "CREATED", "erp_transaction_id": "u-1"}
    seen, client = recorder(201, answer)
    result = client.post_journal_entry(ENTRY, correlation_id="c-9")
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/api/journal-entries"
    assert json.loads(request.content) == ENTRY
    assert request.headers["X-Correlation-ID"] == "c-9"
    assert result["status"] == "CREATED"


def test_post_journal_entry_retry_answer_200_is_returned() -> None:
    _, client = recorder(200, {"status": "ALREADY_EXISTS"})
    assert client.post_journal_entry(ENTRY)["status"] == "ALREADY_EXISTS"


def test_get_journal_entry_escapes_reference_and_returns_body() -> None:
    seen, client = recorder(body={"external_reference": "DSB:a/b"})
    assert client.get_journal_entry("DSB:a/b") == {"external_reference": "DSB:a/b"}
    assert seen[0].url.raw_path == b"/api/journal-entries/DSB%3Aa%2Fb"


def test_get_journal_entry_404_is_none() -> None:
    _, client = recorder(404, erp_error("not_found"))
    assert client.get_journal_entry("NOPE") is None


def test_search_journal_entries_sends_filters_and_returns_items() -> None:
    seen, client = recorder(body={"items": [{"external_reference": "A"}]})
    items = client.search_journal_entries(
        references=["A", "B"],
        date_from=date(2026, 10, 1),
        date_to=date(2026, 10, 31),
        source="REPAYMENT",
        q="loan",
        limit=50,
    )
    assert items == [{"external_reference": "A"}]
    params = seen[0].url.params
    assert params.get_list("reference") == ["A", "B"]
    assert params["from"] == "2026-10-01"
    assert params["to"] == "2026-10-31"
    assert params["source"] == "REPAYMENT"
    assert params["q"] == "loan"
    assert params["limit"] == "50"


def test_search_journal_entries_without_filters_sends_no_params() -> None:
    seen, client = recorder(body={"items": []})
    assert client.search_journal_entries() == []
    assert str(seen[0].url) == f"{BASE}/api/journal-entries"


# --- error mapping -------------------------------------------------------------------------


def test_409_is_conflict_with_erp_body() -> None:
    _, client = recorder(409, erp_error("reference_conflict", "stored with other content"))
    with pytest.raises(ErpConflict) as caught:
        client.post_journal_entry(ENTRY)
    err = caught.value
    assert isinstance(err, ErpRejected)
    assert err.status == 409
    assert err.code == "reference_conflict"
    assert err.message == "stored with other content"
    assert err.details == {"field": "x"}
    assert err.outcome_known is True


@pytest.mark.parametrize("status", [400, 401, 422])
def test_other_4xx_is_rejected(status: int) -> None:
    _, client = recorder(status, erp_error("invalid_request"))
    with pytest.raises(ErpRejected) as caught:
        client.post_journal_entry(ENTRY)
    assert not isinstance(caught.value, ErpConflict)
    assert caught.value.status == status
    assert caught.value.code == "invalid_request"


def test_rejected_with_non_json_body_still_raises() -> None:
    client = make_client(lambda _: httpx.Response(422, text="bad"))
    with pytest.raises(ErpRejected) as caught:
        client.list_accounts()
    assert caught.value.code == ""


def test_429_is_rate_limited_with_retry_after() -> None:
    _, client = recorder(429, erp_error("rate_limited"), headers={"Retry-After": "7"})
    with pytest.raises(ErpRateLimited) as caught:
        client.post_journal_entry(ENTRY)
    assert caught.value.retry_after == 7
    assert caught.value.outcome_known is True


@pytest.mark.parametrize("value", [None, "soon", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_429_without_numeric_retry_after_is_none(value: str | None) -> None:
    headers = {"Retry-After": value} if value is not None else None
    _, client = recorder(429, erp_error("rate_limited"), headers=headers)
    with pytest.raises(ErpRateLimited) as caught:
        client.list_accounts()
    assert caught.value.retry_after is None


def test_503_is_unavailable() -> None:
    _, client = recorder(503, erp_error("erp_unavailable"))
    with pytest.raises(ErpUnavailable) as caught:
        client.post_journal_entry(ENTRY)
    assert caught.value.outcome_known is True


def test_504_is_timeout_with_unknown_outcome() -> None:
    _, client = recorder(504, erp_error("timeout"))
    with pytest.raises(ErpTimeout) as caught:
        client.post_journal_entry(ENTRY)
    assert caught.value.outcome_known is False


def test_500_is_server_error_with_unknown_outcome() -> None:
    _, client = recorder(500, {})
    with pytest.raises(ErpServerError) as caught:
        client.post_journal_entry(ENTRY)
    assert caught.value.outcome_known is False


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.PoolTimeout("p")],
)
def test_connect_failures_are_unavailable(exc: Exception) -> None:
    client = make_client(raising(exc))
    with pytest.raises(ErpUnavailable):
        client.post_journal_entry(ENTRY)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("t"),
        httpx.WriteTimeout("t"),
        httpx.RemoteProtocolError("hung up"),
        httpx.ReadError("reset"),
    ],
)
def test_failures_after_sending_are_timeouts(exc: Exception) -> None:
    client = make_client(raising(exc))
    with pytest.raises(ErpTimeout) as caught:
        client.post_journal_entry(ENTRY)
    assert caught.value.outcome_known is False


def test_calls_are_logged_at_debug_without_the_key(caplog: pytest.LogCaptureFixture) -> None:
    _, client = recorder(body={"items": []})
    with caplog.at_level("DEBUG", logger="integration_service.clients"):
        client.list_accounts()
    assert "/api/accounts" in caplog.text
    assert "erp-key" not in caplog.text
