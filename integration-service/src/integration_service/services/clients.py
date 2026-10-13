"""Clients (Part C): create a client in Fineract keyed by client_uid, and check one.

Create: the same steps as every partner write (see services/loans.py). The client is made
active in one Fineract call, in the Kigali Main Office, with externalId = client_uid and
Idempotency-Key CLI:<external_reference>:<attempt>. The lookup for an unclear outcome is
GET /clients/external-id/{client_uid}.

client_uid is unique, enforced three ways:
- our clients table (primary key) and a check before begin();
- the one-live-client-creation index on operations (another reference while one runs);
- Fineract's unique client externalId. A client that Fineract already holds, and that we
  cannot show to be ours, gives CLIENT_UID_TAKEN. "Ours" means this request had an earlier
  attempt, and the client Fineract holds has the requested name.

Check (GET): reads Fineract, not just our table, and answers whether the client exists, is
active, and has the name we recorded (and, optionally, the name the caller expects)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Engine, insert, select

from integration_service.clients.errors import FineractError, FineractNotFound, FineractRejected
from integration_service.errors import ApiError, ErrorCode
from integration_service.models import clients
from integration_service.schemas.clients import CreateClientRequest
from integration_service.services.fineract_calls import (
    Done,
    Outcome,
    execute_fineract_write,
    look_up_earlier,
)
from integration_service.services.loans import (
    Answer,
    FineractApi,
    answer_for_begin,
    fail,
    fineract_read_error,
    fineract_refusal,
    kigali_today,
    read_or_fail,
    settle,
    status_name,
)
from integration_service.services.operations import (
    New,
    Operation,
    begin,
    mark_failed,
    mark_fineract_done,
    move,
)
from integration_service.services.templates import (
    FINERACT_DATE_FORMAT,
    FINERACT_LOCALE,
    fetch_client_form,
)
from integration_service.state_machine import FINERACT_PENDING, NEEDS_RECONCILIATION

OFFICE_NAME = "Kigali Main Office"
LEGAL_FORM_PERSON = 1
ACTIVE = "ACTIVE"
DUPLICATE_EXTERNAL_ID = "error.msg.client.duplicate.externalId"


@dataclass(frozen=True)
class FineractClientRecord:
    """A client as Fineract holds it. fineract_id is internal: never shown."""

    fineract_id: int
    client_uid: str | None
    first_name: str | None
    last_name: str | None
    display_name: str
    active: bool
    status: str


@dataclass(frozen=True)
class OurClient:
    client_uid: str
    display_name: str
    external_reference: str
    fineract_client_id: int | None = None


# --- pure rules -------------------------------------------------------------------------------


def client_status(code: Any) -> str:
    return status_name(code, "clientStatusType.")


def names_match(a: str, b: str) -> bool:
    """Same name, ignoring case and extra spaces."""

    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    return norm(a) == norm(b)


def build_client_body(request: CreateClientRequest, office_id: int, today: date) -> dict[str, Any]:
    return {
        "officeId": office_id,
        "legalFormId": LEGAL_FORM_PERSON,
        "firstname": request.first_name,
        "lastname": request.last_name,
        "externalId": request.client_uid,
        "active": True,
        "activationDate": today.isoformat(),
        "submittedOnDate": today.isoformat(),
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": FINERACT_LOCALE,
    }


def parse_client(data: Any) -> FineractClientRecord:
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise ValueError("unexpected Fineract client")
    first = data.get("firstname")
    last = data.get("lastname")
    display = data.get("displayName") or " ".join(p for p in (first, last) if p)
    return FineractClientRecord(
        fineract_id=data["id"],
        client_uid=data.get("externalId"),
        first_name=first,
        last_name=last,
        display_name=str(display),
        active=bool(data.get("active")),
        status=client_status((data.get("status") or {}).get("code")),
    )


def creation_body(client_uid: str, name: str, external_reference: str) -> dict[str, Any]:
    return {
        "client_uid": client_uid,
        "name": name,
        "status": ACTIVE,
        "external_reference": external_reference,
    }


def check_body(
    record: FineractClientRecord, ours: OurClient | None, name: str | None
) -> dict[str, Any]:
    return {
        "client_uid": record.client_uid,
        "name": record.display_name,
        "active": record.active,
        "status": record.status,
        "matches_our_record": ours is not None
        and record.active
        and names_match(ours.display_name, record.display_name),
        "name_matches": None if name is None else names_match(name, record.display_name),
    }


def client_rejection(error: FineractRejected, client_uid: str) -> ApiError:
    known = {
        DUPLICATE_EXTERNAL_ID: (
            ErrorCode.CLIENT_UID_TAKEN,
            409,
            f"Fineract already holds a client with client_uid {client_uid}.",
        ),
    }
    return fineract_refusal(error, known, "client")


# --- our records ------------------------------------------------------------------------------


def our_client(engine: Engine, client_uid: str) -> OurClient | None:
    with engine.connect() as conn:
        row = (
            conn.execute(select(clients).where(clients.c.client_uid == client_uid))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    return OurClient(
        client_uid=row["client_uid"],
        display_name=row["display_name"],
        external_reference=row["external_reference"],
        fineract_client_id=row["fineract_client_id"],
    )


# --- create -----------------------------------------------------------------------------------


def find_client(
    fineract: FineractApi, client_uid: str, correlation_id: str | None
) -> FineractClientRecord | None:
    try:
        data = fineract.get(f"/clients/external-id/{client_uid}", correlation_id=correlation_id)
    except FineractNotFound:
        return None
    try:
        return parse_client(data)
    except ValueError as exc:
        raise FineractError("Unexpected Fineract client") from exc


def create_client(
    engine: Engine,
    fineract: FineractApi,
    request: CreateClientRequest,
    partner: str,
    correlation_id: str | None,
    today: date | None = None,
) -> Answer:
    reference = request.external_reference
    uid = request.client_uid
    existing = our_client(engine, uid)
    if existing is not None and existing.external_reference != reference:
        raise ApiError(
            ErrorCode.CLIENT_UID_TAKEN,
            f"client_uid {uid} is already used by another external_reference.",
            409,
        )

    started = begin(
        engine,
        "CLI",
        reference,
        request.model_dump(mode="json"),
        partner,
        correlation_id,
        client_uid=uid,
    )
    if not isinstance(started, New):
        return answer_for_begin(started, reference)
    op = started.operation

    def lookup() -> FineractClientRecord | None:
        return find_client(fineract, uid, correlation_id)

    # A retry looks for its earlier attempt before reading the office: one that landed late
    # is ours, whatever that read would do.
    outcome: Outcome[FineractClientRecord] | None = look_up_earlier(lookup, op.attempts)
    if outcome is None:
        office_id = read_or_fail(
            engine,
            op,
            lambda: _office_id(fineract, correlation_id),
            "the office",
            correlation_id,
        )
        body = build_client_body(request, office_id, today or kigali_today())

        def create(key: str) -> FineractClientRecord:
            data = fineract.post(
                "/clients", body, idempotency_key=key, correlation_id=correlation_id
            )
            client_id = data.get("clientId") if isinstance(data, dict) else None
            if not isinstance(client_id, int):
                client_id = data.get("resourceId") if isinstance(data, dict) else None
            if not isinstance(client_id, int):
                raise FineractError("Unexpected Fineract answer (no client id)")
            return FineractClientRecord(
                fineract_id=client_id,
                client_uid=uid,
                first_name=request.first_name,
                last_name=request.last_name,
                display_name=request.name,
                active=True,
                status=ACTIVE,
            )

        outcome = execute_fineract_write(create, lookup, op.op_key, op.attempts)
    settled = settle(engine, op, outcome, correlation_id)
    if isinstance(settled, Done):
        record = settled.result
        if settled.found_by_lookup and not names_match(record.display_name, request.name):
            fail(engine, op, _taken(uid), correlation_id)
    else:
        error = client_rejection(settled.error, uid)
        found = None
        if error.code == ErrorCode.CLIENT_UID_TAKEN and op.attempts > 1:
            # An earlier attempt of this request may have landed late.
            try:
                found = lookup()
            except FineractError:
                found = None
        if found is None or not names_match(found.display_name, request.name):
            fail(engine, op, error, correlation_id)
        record = found
    return _finish_creation(
        engine,
        op,
        uid,
        request.first_name,
        request.last_name,
        record.fineract_id,
        correlation_id,
    )


def _taken(client_uid: str) -> ApiError:
    return ApiError(
        ErrorCode.CLIENT_UID_TAKEN,
        f"Fineract already holds a client with client_uid {client_uid}.",
        409,
    )


def _office_id(fineract: FineractApi, correlation_id: str | None) -> int:
    """The office id from Fineract's client template (the API user may not read /offices)."""
    office_id = fetch_client_form(fineract, correlation_id).office_id(OFFICE_NAME)
    if office_id is None:
        raise FineractError(f"Fineract has no office named {OFFICE_NAME}")
    return office_id


def _finish_creation(
    engine: Engine,
    op: Operation,
    client_uid: str,
    first_name: str | None,
    last_name: str | None,
    fineract_client_id: int,
    correlation_id: str | None,
) -> Answer:
    name = " ".join(p for p in (first_name, last_name) if p)
    body = creation_body(client_uid, name, op.external_reference)
    with engine.begin() as conn:
        conn.execute(
            insert(clients).values(
                client_uid=client_uid,
                external_reference=op.external_reference,
                display_name=name,
                first_name=first_name,
                last_name=last_name,
                fineract_client_id=fineract_client_id,
                status=ACTIVE,
            )
        )
        mark_fineract_done(
            conn,
            op.id,
            response_status=201,
            response_body=body,
            fineract_resource_id=fineract_client_id,
            detail="client created in Fineract",
            correlation_id=correlation_id,
        )
    return Answer(201, body)


def recover_client_creation(
    engine: Engine, fineract: FineractApi, op: Operation, correlation_id: str | None = None
) -> str:
    """For recovery: a CLI left FINERACT_PENDING. Looks it up; never sends it again.
    Answers what it did: "done", "failed" or "unknown"."""
    assert op.client_uid is not None  # CLI rows always carry one (CHECK constraint)
    try:
        record = find_client(fineract, op.client_uid, correlation_id)
    except FineractError:
        return "unknown"
    if record is None:
        mark_failed(engine, op.id, "recovery lookup found no client", correlation_id)
        return "failed"
    # The same ownership rule as the route: a client found by lookup is ours only if it has
    # the name this request asked for. Otherwise Fineract refused our create as a duplicate.
    asked = op.request_payload if isinstance(op.request_payload, dict) else {}
    first, last = asked.get("first_name"), asked.get("last_name")
    if not isinstance(first, str) or not isinstance(last, str):
        move(
            engine,
            op.id,
            NEEDS_RECONCILIATION,
            "recovery: Fineract holds this client_uid, but the requested name is not on "
            "record, so it cannot be matched; check by hand",
            correlation_id,
            expected=FINERACT_PENDING,
            last_error="client found in Fineract; requested name unknown",
        )
        return "done"
    if not names_match(record.display_name, f"{first} {last}"):
        mark_failed(
            engine,
            op.id,
            f"client_uid {op.client_uid} is already held in Fineract by another client; "
            "nothing was created for this request",
            correlation_id,
        )
        return "failed"
    _finish_creation(
        engine,
        op,
        op.client_uid,
        first,
        last,
        record.fineract_id,
        correlation_id,
    )
    return "done"


# --- check ------------------------------------------------------------------------------------


def check_client(
    engine: Engine,
    fineract: FineractApi,
    client_uid: str,
    name: str | None,
    correlation_id: str | None,
) -> dict[str, Any]:
    """Part C: does Fineract hold this client, is it active, is the name right."""
    try:
        record = find_client(fineract, client_uid, correlation_id)
    except FineractError as exc:
        raise fineract_read_error(exc) from exc
    if record is None:
        raise ApiError(ErrorCode.CLIENT_NOT_FOUND, f"Fineract has no client {client_uid}.", 404)
    return check_body(record, our_client(engine, client_uid), name)
