"""Pure rules for clients: request validation, the Fineract body, reading Fineract's answer,
the partner answers (never a Fineract id) and how Fineract refusals become partner errors."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from integration_service.clients.errors import FineractRejected
from integration_service.errors import ErrorCode
from integration_service.schemas.clients import ClientPath, CreateClientRequest
from integration_service.services.clients import (
    FineractClientRecord,
    OurClient,
    build_client_body,
    check_body,
    client_rejection,
    client_status,
    creation_body,
    find_office_id,
    names_match,
    parse_client,
)

ID_KEY = re.compile(r"(^id$|_id$|Id$)")


def id_keys(value: Any) -> list[str]:
    """Every key in a JSON-like value that looks like an id, our own references aside."""
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


def valid(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "external_reference": "CLI-REF-1",
        "client_uid": "SME-000001",
        "first_name": "Testa",
        "last_name": "Fakename",
    }
    body.update(changes)
    return body


FINERACT_CLIENT = {
    "id": 41,
    "accountNo": "000000041",
    "externalId": "SME-000001",
    "status": {"id": 300, "code": "clientStatusType.active", "value": "Active"},
    "active": True,
    "firstname": "Testa",
    "lastname": "Fakename",
    "displayName": "Testa Fakename",
    "officeId": 2,
    "officeName": "Kigali Main Office",
}


# --- request validation ------------------------------------------------------------------


def test_a_good_request_is_accepted_and_names_are_trimmed() -> None:
    request = CreateClientRequest.model_validate(valid(first_name="  Testa "))
    assert request.first_name == "Testa"
    assert request.name == "Testa Fakename"


@pytest.mark.parametrize(
    "client_uid", ["SME-1", "sme-000001", "SME000001", "SME-0000001", "S-000001", "", "SME-00000A"]
)
def test_bad_client_uid_is_refused(client_uid: str) -> None:
    with pytest.raises(ValidationError):
        CreateClientRequest.model_validate(valid(client_uid=client_uid))


@pytest.mark.parametrize("client_uid", ["SME-000001", "AGRI-123456", "AB-000000"])
def test_good_client_uids(client_uid: str) -> None:
    assert CreateClientRequest.model_validate(valid(client_uid=client_uid)).client_uid == client_uid


@pytest.mark.parametrize(
    "changes",
    [
        {"external_reference": ""},
        {"external_reference": "x" * 41},
        {"external_reference": "bad ref"},
        {"first_name": ""},
        {"first_name": "   "},
        {"last_name": "x" * 51},
        {"first_name": "Testa1"},
        {"last_name": "<script>"},
        {"office_id": 2},
    ],
)
def test_bad_fields_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CreateClientRequest.model_validate(valid(**changes))


@pytest.mark.parametrize("missing", ["external_reference", "client_uid", "first_name", "last_name"])
def test_every_field_is_required(missing: str) -> None:
    body = valid()
    del body[missing]
    with pytest.raises(ValidationError):
        CreateClientRequest.model_validate(body)


def test_names_with_accents_apostrophes_and_hyphens_are_fine() -> None:
    request = CreateClientRequest.model_validate(
        valid(first_name="Ánge-Marie", last_name="O'Testcase")
    )
    assert request.name == "Ánge-Marie O'Testcase"


def test_path_checks_the_client_uid() -> None:
    assert ClientPath(client_uid="SME-000001").client_uid == "SME-000001"
    with pytest.raises(ValidationError):
        ClientPath(client_uid="1")


# --- Fineract body and answer ------------------------------------------------------------


def test_fineract_body_creates_an_active_client_keyed_by_client_uid() -> None:
    request = CreateClientRequest.model_validate(valid())
    body = build_client_body(request, office_id=2, today=date(2026, 10, 13))
    assert body == {
        "officeId": 2,
        "legalFormId": 1,
        "firstname": "Testa",
        "lastname": "Fakename",
        "externalId": "SME-000001",
        "active": True,
        "activationDate": "2026-10-13",
        "submittedOnDate": "2026-10-13",
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }


def test_office_is_found_by_name_in_the_client_template() -> None:
    template = {
        "officeId": 1,
        "officeOptions": [
            {"id": 1, "name": "Head Office"},
            {"id": 2, "name": "Kigali Main Office"},
        ],
    }
    assert find_office_id(template, "Kigali Main Office") == 2


def test_missing_office_is_a_lookup_error() -> None:
    with pytest.raises(LookupError):
        find_office_id({"officeOptions": [{"id": 1, "name": "Head Office"}]}, "Kigali Main Office")


def test_parse_client_reads_fineract_fields() -> None:
    record = parse_client(FINERACT_CLIENT)
    assert record == FineractClientRecord(
        fineract_id=41,
        client_uid="SME-000001",
        first_name="Testa",
        last_name="Fakename",
        display_name="Testa Fakename",
        active=True,
        status="ACTIVE",
    )


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("clientStatusType.active", "ACTIVE"),
        ("clientStatusType.pending", "PENDING"),
        ("clientStatusType.closed", "CLOSED"),
        ("clientStatusType.transfer.in.progress", "TRANSFER_IN_PROGRESS"),
        (None, "UNKNOWN"),
    ],
)
def test_client_status_names(code: str | None, status: str) -> None:
    assert client_status(code) == status


def test_creation_answer_has_our_identifiers_only() -> None:
    body = creation_body("SME-000001", "Testa Fakename", "CLI-REF-1")
    assert body == {
        "client_uid": "SME-000001",
        "name": "Testa Fakename",
        "status": "ACTIVE",
        "external_reference": "CLI-REF-1",
    }
    assert id_keys(body) == []


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Testa Fakename", "testa  fakename", True),
        (" Testa Fakename ", "Testa Fakename", True),
        ("Testa Fakename", "Testa Fakenam", False),
    ],
)
def test_names_match_ignores_case_and_spacing(a: str, b: str, same: bool) -> None:
    assert names_match(a, b) is same


def test_check_body_matches_our_record() -> None:
    ours = OurClient(client_uid="SME-000001", display_name="Testa Fakename", external_reference="R")
    body = check_body(parse_client(FINERACT_CLIENT), ours, name="TESTA fakename")
    assert body == {
        "client_uid": "SME-000001",
        "name": "Testa Fakename",
        "active": True,
        "status": "ACTIVE",
        "matches_our_record": True,
        "name_matches": True,
    }
    assert id_keys(body) == []


def test_check_body_without_our_record_or_with_another_name() -> None:
    record = parse_client(FINERACT_CLIENT)
    assert check_body(record, None, name=None)["matches_our_record"] is False
    assert check_body(record, None, name=None)["name_matches"] is None
    other = OurClient(client_uid="SME-000001", display_name="Other Name", external_reference="R")
    body = check_body(record, other, name="Someone Else")
    assert body["matches_our_record"] is False
    assert body["name_matches"] is False


def test_inactive_client_does_not_match_our_record() -> None:
    record = parse_client(
        {**FINERACT_CLIENT, "active": False, "status": {"code": "clientStatusType.closed"}}
    )
    ours = OurClient(client_uid="SME-000001", display_name="Testa Fakename", external_reference="R")
    body = check_body(record, ours, name=None)
    assert body["active"] is False
    assert body["status"] == "CLOSED"
    assert body["matches_our_record"] is False


# --- Fineract refusals -------------------------------------------------------------------


def rejected(
    status: int, *codes: str, message: str = "Client with id 41 exists"
) -> FineractRejected:
    body: dict[str, Any] = {
        "httpStatusCode": str(status),
        "developerMessage": message,
        "defaultUserMessage": message,
        "errors": [{"userMessageGlobalisationCode": c, "developerMessage": message} for c in codes],
    }
    return FineractRejected(status, body)


def test_duplicate_external_id_is_client_uid_taken() -> None:
    error = client_rejection(rejected(403, "error.msg.client.duplicate.externalId"), "SME-000001")
    assert error.code == ErrorCode.CLIENT_UID_TAKEN
    assert error.status == 409
    assert "SME-000001" in error.message


def test_unknown_refusal_is_a_validation_error_without_fineract_text() -> None:
    error = client_rejection(rejected(400, "validation.msg.something"), "SME-000001")
    assert error.code == ErrorCode.VALIDATION_ERROR
    assert error.status == 422
    assert "41" not in error.message
    assert "41" not in str(error.details)
    assert error.details == {"fineract_codes": ["validation.msg.something"]}


def test_our_own_credentials_refused_is_not_the_partners_fault() -> None:
    error = client_rejection(rejected(403, "error.msg.not.authorized"), "SME-000001")
    assert error.code == ErrorCode.FINERACT_UNAVAILABLE
    assert error.status == 502
    error = client_rejection(rejected(401), "SME-000001")
    assert error.status == 502
