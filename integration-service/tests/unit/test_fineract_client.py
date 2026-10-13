"""The Fineract client, against a fake transport: headers, paths and error mapping."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from integration_service.clients.errors import (
    FineractConflict,
    FineractNotFound,
    FineractRejected,
    FineractServerError,
    FineractTimeout,
    FineractUnavailable,
    InvalidIdempotencyKey,
)
from integration_service.clients.fineract import ErpAccountSyncResult, FineractClient

BASE = "http://fineract.test/fineract-provider/api/v1"
Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> FineractClient:
    return FineractClient(
        base_url=BASE,
        tenant="default",
        username="mifos",
        password="password",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def recorder(status: int = 200, body: Any = None) -> tuple[list[httpx.Request], FineractClient]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {})

    return seen, make_client(handler)


def fineract_error(status: int, code: str, inner: str) -> dict[str, Any]:
    return {
        "developerMessage": "Request was understood but caused a domain rule violation.",
        "httpStatusCode": str(status),
        "defaultUserMessage": "Errors contain reason for domain rule violation.",
        "userMessageGlobalisationCode": code,
        "errors": [
            {
                "developerMessage": f"detail for {inner}",
                "defaultUserMessage": f"detail for {inner}",
                "userMessageGlobalisationCode": inner,
                "parameterName": "id",
            }
        ],
    }


SYNC_BODY = {
    "name": "Loan Receivable",
    "glCode": "1200",
    "type": 1,
    "usage": 1,
    "manualEntriesAllowed": False,
    "parentAccount": "GL-1000",
}


# --- headers -------------------------------------------------------------------------------


def test_every_call_sends_tenant_basic_auth_and_json() -> None:
    seen, client = recorder(body={"ok": True})
    client.get("/glaccounts")
    request = seen[0]
    assert request.headers["Fineract-Platform-TenantId"] == "default"
    expected = base64.b64encode(b"mifos:password").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert request.headers["Accept"] == "application/json"
    assert str(request.url) == f"{BASE}/glaccounts"


def test_post_sends_idempotency_key_and_json_body() -> None:
    seen, client = recorder(body={"resourceId": 5})
    result = client.post("/clients", {"a": 1}, idempotency_key="CLI:abc")
    request = seen[0]
    assert request.method == "POST"
    assert request.headers["Idempotency-Key"] == "CLI:abc"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {"a": 1}
    assert result == {"resourceId": 5}


def test_post_without_idempotency_key_sends_no_header() -> None:
    seen, client = recorder()
    client.post("/clients", {"a": 1})
    assert "Idempotency-Key" not in seen[0].headers


def test_put_sends_idempotency_key_and_query_params() -> None:
    seen, client = recorder(body={"changes": {}})
    client.put("/loans/1", {"x": 2}, idempotency_key="k1", params={"command": "approve"})
    request = seen[0]
    assert request.method == "PUT"
    assert request.headers["Idempotency-Key"] == "k1"
    assert request.url.params["command"] == "approve"


def test_get_passes_query_params() -> None:
    seen, client = recorder(body=[])
    assert client.get("/glaccounts", {"usage": 1}) == []
    assert seen[0].url.params["usage"] == "1"


def test_correlation_id_is_passed_through_when_given() -> None:
    seen, client = recorder()
    client.post("/clients", {}, correlation_id="c-123")
    assert seen[0].headers["X-Correlation-ID"] == "c-123"


def test_idempotency_key_longer_than_50_chars_is_refused_before_sending() -> None:
    seen, client = recorder()
    with pytest.raises(InvalidIdempotencyKey):
        client.post("/clients", {}, idempotency_key="x" * 51)
    assert seen == []


def test_idempotency_key_of_exactly_50_chars_is_sent() -> None:
    seen, client = recorder()
    client.post("/clients", {}, idempotency_key="x" * 50)
    assert seen[0].headers["Idempotency-Key"] == "x" * 50


def test_empty_idempotency_key_is_refused() -> None:
    _, client = recorder()
    with pytest.raises(InvalidIdempotencyKey):
        client.post("/clients", {}, idempotency_key="")


# --- ERP account sync ----------------------------------------------------------------------


def test_put_erp_account_sends_body_to_the_plugin_path_and_parses_result() -> None:
    seen, client = recorder(
        body={
            "erpReference": "GL-1200",
            "glAccountId": 7,
            "glCode": "1200",
            "outcome": "CREATED",
            "parentAccount": "GL-1000",
        }
    )
    result = client.put_erp_account("GL-1200", SYNC_BODY, idempotency_key="GL-1200")
    request = seen[0]
    assert request.method == "PUT"
    assert str(request.url) == f"{BASE}/erp-integration/accounts/GL-1200"
    assert json.loads(request.content) == SYNC_BODY
    assert request.headers["Idempotency-Key"] == "GL-1200"
    assert result == ErpAccountSyncResult(
        erp_reference="GL-1200", gl_account_id=7, gl_code="1200", outcome="CREATED"
    )


def test_put_erp_account_reads_unchanged() -> None:
    _, client = recorder(
        body={"erpReference": "GL-1", "glAccountId": 3, "glCode": "1", "outcome": "UNCHANGED"}
    )
    assert client.put_erp_account("GL-1", SYNC_BODY).outcome == "UNCHANGED"


def test_put_erp_account_conflict_raises_with_codes() -> None:
    _, client = recorder(
        409,
        fineract_error(
            409,
            "error.msg.glaccount.sync.code.conflict",
            "error.msg.glaccount.sync.code.conflict",
        ),
    )
    with pytest.raises(FineractConflict) as caught:
        client.put_erp_account("GL-1200", SYNC_BODY)
    err = caught.value
    assert isinstance(err, FineractRejected)
    assert err.status == 409
    assert "error.msg.glaccount.sync.code.conflict" in err.error_codes
    assert err.developer_message


def test_erp_reference_is_url_escaped() -> None:
    seen, client = recorder(
        body={
            "erpReference": "a/b",
            "glAccountId": 1,
            "glCode": "1",
            "name": "N",
            "type": "ASSET",
            "usage": "DETAIL",
        }
    )
    client.get_erp_account("a/b")
    assert seen[0].url.raw_path.endswith(b"/erp-integration/accounts/a%2Fb")


def test_get_erp_account_returns_the_mapping() -> None:
    body = {
        "erpReference": "GL-1200",
        "glAccountId": 7,
        "glCode": "1200",
        "name": "Loan Receivable",
        "type": "ASSET",
        "usage": "DETAIL",
        "parentAccount": "GL-1000",
        "hierarchy": ".3.7.",
        "syncedAt": "2026-10-12T14:36:58Z",
    }
    seen, client = recorder(body=body)
    account = client.get_erp_account("GL-1200")
    assert seen[0].method == "GET"
    assert account is not None
    assert account.erp_reference == "GL-1200"
    assert account.gl_account_id == 7
    assert account.gl_code == "1200"
    assert account.name == "Loan Receivable"
    assert account.type == "ASSET"
    assert account.usage == "DETAIL"
    assert account.parent_account == "GL-1000"


def test_get_erp_account_404_is_none() -> None:
    _, client = recorder(
        404, fineract_error(404, "error.msg.resource.not.found", "error.msg.erpaccount.not.found")
    )
    assert client.get_erp_account("NOPE") is None


# --- error mapping -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, FineractRejected),
        (403, FineractRejected),
        (404, FineractNotFound),
        (409, FineractConflict),
        (425, FineractRejected),
    ],
)
def test_4xx_maps_to_rejected_family(status: int, expected: type[Exception]) -> None:
    _, client = recorder(status, fineract_error(status, "error.msg.x", "error.msg.inner"))
    with pytest.raises(expected) as caught:
        client.get("/anything")
    err = caught.value
    assert isinstance(err, FineractRejected)
    assert err.status == status
    assert err.error_codes == ["error.msg.x", "error.msg.inner"]
    assert err.body["httpStatusCode"] == str(status)


def test_404_is_not_a_conflict_and_409_is_not_not_found() -> None:
    assert not issubclass(FineractNotFound, FineractConflict)
    assert not issubclass(FineractConflict, FineractNotFound)


def test_rejected_with_non_json_body_still_raises() -> None:
    client = make_client(lambda _: httpx.Response(400, text="<html>bad</html>"))
    with pytest.raises(FineractRejected) as caught:
        client.get("/x")
    assert caught.value.error_codes == []
    assert caught.value.body == {}


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_server_error_with_unknown_outcome(status: int) -> None:
    _, client = recorder(status, {"developerMessage": "boom"})
    with pytest.raises(FineractServerError) as caught:
        client.post("/clients", {})
    assert caught.value.status == status
    assert caught.value.outcome_known is False


def raising(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("connect timed out")],
)
def test_connect_failures_mean_not_sent(exc: Exception) -> None:
    client = make_client(raising(exc))
    with pytest.raises(FineractUnavailable) as caught:
        client.post("/clients", {})
    assert caught.value.outcome_known is True


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.RemoteProtocolError("server hung up"),
        httpx.ReadError("connection reset"),
    ],
)
def test_failures_after_sending_mean_outcome_unknown(exc: Exception) -> None:
    client = make_client(raising(exc))
    with pytest.raises(FineractTimeout) as caught:
        client.post("/clients", {})
    assert caught.value.outcome_known is False


def test_pool_timeout_means_not_sent() -> None:
    client = make_client(raising(httpx.PoolTimeout("no free connection")))
    with pytest.raises(FineractUnavailable):
        client.get("/x")


# --- health --------------------------------------------------------------------------------


def test_health_calls_actuator_outside_the_api_prefix() -> None:
    seen, client = recorder(body={"status": "UP"})
    assert client.health() is True
    assert str(seen[0].url) == "http://fineract.test/fineract-provider/actuator/health"


def test_health_is_false_when_down_or_unreachable() -> None:
    _, down = recorder(503, {"status": "DOWN"})
    assert down.health() is False
    _, odd = recorder(200, {"status": "DOWN"})
    assert odd.health() is False
    assert make_client(raising(httpx.ConnectError("refused"))).health() is False


# --- logging -------------------------------------------------------------------------------


def test_calls_are_logged_at_debug_without_secrets(caplog: pytest.LogCaptureFixture) -> None:
    _, client = recorder(body={})
    with caplog.at_level("DEBUG", logger="integration_service.clients"):
        client.post("/clients", {"firstname": "A"}, idempotency_key="CLI:1")
    text = caplog.text
    assert "POST" in text
    assert "/clients" in text
    assert "password" not in text
    assert base64.b64encode(b"mifos:password").decode() not in text


def test_unreadable_plugin_answer_does_not_copy_the_body_into_the_message() -> None:
    # outcome is missing, so the answer cannot be read. Its values (a Fineract id) must not
    # end up in the message, which can travel to a partner through last_error.
    _, client = recorder(200, {"erpReference": "GL-1200", "glAccountId": 98765, "glCode": "1"})
    with pytest.raises(FineractServerError) as caught:
        client.put_erp_account("GL-1200", {"name": "x"})
    assert "98765" not in caught.value.message
    assert caught.value.message == "Unexpected Fineract answer (unreadable body)"
    assert caught.value.body["glAccountId"] == 98765  # still there for our own logs
