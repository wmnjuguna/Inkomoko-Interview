"""POST /api/clients and GET /api/clients/{client_uid} against the running Fineract, as the
least-privilege API user (FINERACT_API_USERNAME / FINERACT_API_PASSWORD).

Fineract is shared and keeps its data, so every test uses its own client_uid and
external_reference, made fresh for each run. Nothing is deleted. The names are obviously
fake test names."""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine, text
from test_disburse_api import Flaky as TemplateFlaky

from integration_service.clients.errors import FineractTimeout, FineractUnavailable
from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.errors import ApiError, ErrorCode
from integration_service.schemas.clients import CreateClientRequest
from integration_service.services.clients import create_client, recover_client_creation
from integration_service.services.loans import fineract_from_settings
from integration_service.services.operations import get_operation

ID_KEY = re.compile(r"(^id$|_id$|Id$)")


@pytest.fixture
def settings(settings: Settings) -> Settings:
    password = os.environ.get("FINERACT_API_PASSWORD", "")
    if not password:
        pytest.skip("FINERACT_API_PASSWORD is not set (the API user's password from .env)")
    return replace(
        settings,
        fineract_username=os.environ.get("FINERACT_API_USERNAME", "integration-api"),
        fineract_password=password,
    )


@pytest.fixture(autouse=True)
def _clean(db: Engine) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE outbox, operation_events, loans, clients, operations "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def fineract(settings: Settings) -> Iterator[FineractClient]:
    client = fineract_from_settings(settings)
    yield client
    client.close()


@pytest.fixture
def admin(settings: Settings) -> Iterator[FineractClient]:
    client = FineractClient(
        base_url=settings.fineract_base_url,
        tenant=settings.fineract_tenant,
        username=os.environ.get("FINERACT_USERNAME", "mifos"),
        password=os.environ.get("FINERACT_PASSWORD", "password"),
        timeout_seconds=30,
    )
    yield client
    client.close()


def fresh() -> tuple[str, str]:
    """A client_uid and a reference no earlier run used."""
    n = secrets.randbelow(10**6)
    tag = secrets.token_hex(3)
    return f"ITC-{n:06d}", f"IT-CLI-{tag}-{n}"


def body(uid: str, ref: str, first: str = "Testa", last: str = "Fakename") -> dict[str, Any]:
    return {"external_reference": ref, "client_uid": uid, "first_name": first, "last_name": last}


def post(client: FlaskClient, auth: dict[str, str], data: dict[str, Any]) -> Any:
    response = client.post("/api/clients", json=data, headers=auth)
    no_ids(response.get_json())
    return response


def history(client: FlaskClient, auth: dict[str, str], ref: str) -> dict[str, Any]:
    response = client.get(f"/api/transactions/{ref}", headers=auth)
    assert response.status_code == 200
    data: dict[str, Any] = response.get_json()
    no_ids(data)
    return data


# --- create ------------------------------------------------------------------------------------


def test_create_replay_conflict_and_taken(client: FlaskClient, auth: dict[str, str]) -> None:
    uid, ref = fresh()

    first = post(client, auth, body(uid, ref))
    assert first.status_code == 201
    assert first.get_json() == {
        "client_uid": uid,
        "name": "Testa Fakename",
        "status": "ACTIVE",
        "external_reference": ref,
    }

    again = post(client, auth, body(uid, ref))
    assert again.status_code == 200
    assert again.get_json() == first.get_json()

    other_data = post(client, auth, body(uid, ref, first="Otherfirst"))
    assert other_data.status_code == 409
    assert other_data.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    other_ref = post(client, auth, body(uid, ref + "-B"))
    assert other_ref.status_code == 409
    assert other_ref.get_json()["error"]["code"] == "CLIENT_UID_TAKEN"

    op = history(client, auth, ref)["operations"][0]
    assert op["type"] == "CLIENT_CREATION"
    assert op["status"] == "COMPLETED"
    assert op["attempts"] == 1


def test_client_uid_held_in_fineract_by_someone_else_is_taken(
    client: FlaskClient, auth: dict[str, str], admin: FineractClient
) -> None:
    uid, ref = fresh()
    office = next(o["id"] for o in admin.get("/offices") if o.get("externalId") == "OFF-KGL-MAIN")
    admin.post(
        "/clients",
        {
            "officeId": office,
            "legalFormId": 1,
            "firstname": "Outside",
            "lastname": "Testperson",
            "externalId": uid,
            "active": True,
            "activationDate": "2026-01-01",
            "submittedOnDate": "2026-01-01",
            "dateFormat": "yyyy-MM-dd",
            "locale": "en",
        },
    )

    response = post(client, auth, body(uid, ref))

    assert response.status_code == 409
    error = response.get_json()["error"]
    assert error["code"] == "CLIENT_UID_TAKEN"
    assert uid in error["message"]
    op = history(client, auth, ref)["operations"][0]
    assert op["status"] == "FAILED"
    assert op["last_error"] == error["message"]

    check = client.get(f"/api/clients/{uid}", headers=auth)
    no_ids(check.get_json())
    assert check.status_code == 200
    assert check.get_json()["matches_our_record"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"client_uid": "nope"},
        {"external_reference": "has space"},
        {"first_name": ""},
        {"fineract_client_id": 1},
    ],
)
def test_bad_body_is_422(
    client: FlaskClient, auth: dict[str, str], changes: dict[str, Any]
) -> None:
    uid, ref = fresh()
    response = post(client, auth, {**body(uid, ref), **changes})
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"


def test_missing_reference_is_422(client: FlaskClient, auth: dict[str, str]) -> None:
    uid, ref = fresh()
    data = body(uid, ref)
    del data["external_reference"]
    response = post(client, auth, data)
    assert response.status_code == 422


# --- check -------------------------------------------------------------------------------------


def test_check_reads_fineract(client: FlaskClient, auth: dict[str, str]) -> None:
    uid, ref = fresh()
    assert post(client, auth, body(uid, ref)).status_code == 201

    response = client.get(f"/api/clients/{uid}", headers=auth)
    no_ids(response.get_json())
    assert response.status_code == 200
    assert response.get_json() == {
        "client_uid": uid,
        "name": "Testa Fakename",
        "active": True,
        "status": "ACTIVE",
        "matches_our_record": True,
        "name_matches": None,
    }

    right = client.get(f"/api/clients/{uid}?name=testa%20FAKENAME", headers=auth)
    wrong = client.get(f"/api/clients/{uid}?name=Somebody%20Else", headers=auth)
    no_ids(right.get_json())
    no_ids(wrong.get_json())
    assert right.get_json()["name_matches"] is True
    assert wrong.get_json()["name_matches"] is False


def test_unknown_client_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    uid, _ = fresh()
    response = client.get(f"/api/clients/{uid}", headers=auth)
    no_ids(response.get_json())
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "CLIENT_NOT_FOUND"


def test_malformed_client_uid_is_422(client: FlaskClient, auth: dict[str, str]) -> None:
    assert client.get("/api/clients/42", headers=auth).status_code == 422


# --- unclear outcomes, through the service with a Fineract stand-in ------------------------------


class Flaky:
    """The real Fineract, with one failure on the first write.
    mode "lost_answer": the write reaches Fineract, then the answer is lost (timeout).
    mode "down": the write never leaves (not reachable).
    mode "blind": the write never leaves but looks like a timeout, and lookups fail too."""

    def __init__(self, real: FineractClient, mode: str) -> None:
        self.real = real
        self.mode = mode
        self.keys: list[str | None] = []
        self.writes = 0

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        if self.mode == "blind" and "/external-id/" in path:
            raise FineractTimeout("no answer")
        return self.real.get(path, params=params, correlation_id=correlation_id)

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.keys.append(idempotency_key)
        self.writes += 1
        if self.mode == "down":
            raise FineractUnavailable("not reachable")
        if self.mode == "blind":
            raise FineractTimeout("no answer")
        self.real.post(path, body, idempotency_key, params, correlation_id)
        raise FineractTimeout("answer lost")


def request(uid: str, ref: str) -> CreateClientRequest:
    return CreateClientRequest.model_validate(body(uid, ref))


def test_lost_answer_is_found_by_lookup_and_not_sent_again(
    db: Engine, fineract: FineractClient
) -> None:
    uid, ref = fresh()
    flaky = Flaky(fineract, "lost_answer")

    answer = create_client(db, flaky, request(uid, ref), "partner-a", "c-lost")

    assert answer.status == 201
    assert flaky.writes == 1
    assert flaky.keys == [f"CLI:{ref}:1"]
    op = get_operation(db, "CLI", ref)
    assert op is not None and op.status == "COMPLETED"
    assert fineract.get(f"/clients/external-id/{uid}")["displayName"] == "Testa Fakename"


def test_unreachable_fails_then_the_same_reference_retries_with_the_next_key(
    db: Engine, fineract: FineractClient
) -> None:
    uid, ref = fresh()
    down = Flaky(fineract, "down")
    with pytest.raises(ApiError) as caught:
        create_client(db, down, request(uid, ref), "partner-a", "c-down")
    assert caught.value.status == 503
    assert caught.value.code == ErrorCode.FINERACT_UNAVAILABLE
    op = get_operation(db, "CLI", ref)
    assert op is not None and op.status == "FAILED"

    answer = create_client(db, fineract, request(uid, ref), "partner-a", "c-retry")

    assert answer.status == 201
    op = get_operation(db, "CLI", ref)
    assert op is not None and op.status == "COMPLETED" and op.attempts == 2
    assert down.keys == [f"CLI:{ref}:1"]


def test_a_retry_finds_the_earlier_attempt_before_reading_the_client_template(
    db: Engine, fineract: FineractClient
) -> None:
    """Attempt 1 reached Fineract although we heard "not reachable", so it was FAILED. On
    the retry the client template read fails: the client is looked up first and finished."""
    uid, ref = fresh()
    fake = TemplateFlaky(
        fineract, FineractUnavailable("lost (test)"), fail_posts=1, after_send=True
    )
    with pytest.raises(ApiError) as caught:
        create_client(db, fake, request(uid, ref), "partner-a", None)
    assert caught.value.status == 503
    reads = fake.template_reads

    fake.fail_templates = 1
    answer = create_client(db, fake, request(uid, ref), "partner-a", None)
    assert answer.status == 201
    assert fake.template_reads == reads
    assert fake.post_keys == [f"CLI:{ref}:1"]


def test_unclear_with_failed_lookup_stays_pending_then_recovery_decides(
    db: Engine, fineract: FineractClient, client: FlaskClient, auth: dict[str, str]
) -> None:
    uid, ref = fresh()
    blind = Flaky(fineract, "blind")
    with pytest.raises(ApiError) as caught:
        create_client(db, blind, request(uid, ref), "partner-a", "c-blind")
    assert caught.value.status == 504
    assert caught.value.code == ErrorCode.FINERACT_TIMEOUT
    op = get_operation(db, "CLI", ref)
    assert op is not None and op.status == "FINERACT_PENDING"

    repeat = post(client, auth, body(uid, ref))
    assert repeat.status_code == 202
    assert repeat.get_json()["status"] == "FINERACT_PENDING"
    assert blind.writes == 1  # the repeat sent nothing

    assert recover_client_creation(db, fineract, op) == "failed"
    op = get_operation(db, "CLI", ref)
    assert op is not None and op.status == "FAILED"


def test_recovery_finishes_a_client_fineract_holds(db: Engine, fineract: FineractClient) -> None:
    uid, ref = fresh()
    flaky = Flaky(fineract, "lost_answer")
    blind_lookup = Flaky(fineract, "blind")
    # The write lands, the answer is lost, and the lookup fails: unclear.
    flaky.get = blind_lookup.get  # type: ignore[method-assign]
    with pytest.raises(ApiError):
        create_client(db, flaky, request(uid, ref), "partner-a", "c-rec")
    op = get_operation(db, "CLI", ref)
    assert op is not None and op.status == "FINERACT_PENDING"

    assert recover_client_creation(db, fineract, op) == "done"

    op = get_operation(db, "CLI", ref)
    assert op is not None and op.status == "COMPLETED"
    assert op.response_body == {
        "client_uid": uid,
        "name": "Testa Fakename",
        "status": "ACTIVE",
        "external_reference": ref,
    }


# --- no Fineract ids anywhere ---------------------------------------------------------------


def id_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if ID_KEY.search(key) and key != "correlation_id":
                found.append(key)
            found += id_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            found += id_keys(inner)
    return found


def no_ids(body: Any) -> None:
    """Checked on every answer as it comes, success or error: no Fineract id key."""
    assert id_keys(body) == [], body
