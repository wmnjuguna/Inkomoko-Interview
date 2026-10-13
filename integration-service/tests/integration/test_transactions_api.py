"""GET /api/transactions/{external_reference}: what happened to a partner request.
Operations are made with the engine directly; no Fineract calls."""

from __future__ import annotations

import json
import logging

import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine, text

from integration_service.services.operations import (
    New,
    begin,
    mark_failed,
    mark_fineract_done,
)

LOAN = "LN-2026-000077"
FINERACT_LOAN_ID = 987654321
FINERACT_TRANSACTION_ID = 876543219


@pytest.fixture(autouse=True)
def _clean(db: Engine) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE outbox, operation_events, loans, clients, operations "
                "RESTART IDENTITY CASCADE"
            )
        )


def disbursement(db: Engine, reference: str, partner: str = "partner-a") -> New:
    result = begin(db, "DSB", reference, {"amount": 1}, partner, "c-1", loan_reference=LOAN)
    assert isinstance(result, New)
    return result


def test_shows_every_operation_with_its_history(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    dsb = disbursement(db, "FIN-77").operation
    with db.begin() as conn:
        mark_fineract_done(
            conn,
            dsb.id,
            response_status=200,
            response_body={"ok": True},
            fineract_resource_id=FINERACT_LOAN_ID,
            fineract_transaction_id=FINERACT_TRANSACTION_ID,
            outbox_entry={"lines": []},
        )
    rpy = begin(db, "RPY", "FIN-77", {"amount": 2}, "partner-a", "c-2", loan_reference=LOAN)
    assert isinstance(rpy, New)
    mark_failed(db, rpy.operation.id, "Fineract not reachable; nothing was sent")

    response = client.get("/api/transactions/FIN-77", headers=auth)

    assert response.status_code == 200
    body = response.get_json()
    assert body["external_reference"] == "FIN-77"
    first, second = body["operations"]
    assert first["type"] == "DISBURSEMENT"
    assert first["op_key"] == "DSB:FIN-77"
    assert first["status"] == "ERP_PENDING"
    assert first["erp_status"] == "PENDING"
    assert first["loan_reference"] == LOAN
    assert first["client_uid"] is None
    assert first["attempts"] == 1
    assert first["last_error"] is None
    assert [(h["from"], h["to"]) for h in first["history"]] == [
        (None, "RECEIVED"),
        ("RECEIVED", "FINERACT_PENDING"),
        ("FINERACT_PENDING", "FINERACT_DONE"),
        ("FINERACT_DONE", "ERP_PENDING"),
    ]
    assert all(h["at"] and "detail" in h for h in first["history"])
    assert first["created_at"] and first["updated_at"]
    assert second["type"] == "REPAYMENT"
    assert second["status"] == "FAILED"
    assert second["last_error"] == "Fineract not reachable; nothing was sent"
    assert second["history"][-1]["detail"] == "Fineract not reachable; nothing was sent"


def test_never_shows_fineract_ids(client: FlaskClient, auth: dict[str, str], db: Engine) -> None:
    dsb = disbursement(db, "FIN-78").operation
    with db.begin() as conn:
        mark_fineract_done(
            conn,
            dsb.id,
            response_status=200,
            response_body={"ok": True},
            fineract_resource_id=FINERACT_LOAN_ID,
            fineract_transaction_id=FINERACT_TRANSACTION_ID,
        )
    raw = client.get("/api/transactions/FIN-78", headers=auth).get_data(as_text=True)
    assert str(FINERACT_LOAN_ID) not in raw
    assert str(FINERACT_TRANSACTION_ID) not in raw
    assert "fineract_" not in json.dumps(json.loads(raw))
    assert "response_body" not in raw


def test_unknown_reference_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get("/api/transactions/NOPE-1", headers=auth)
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "NOT_FOUND"
    assert "NOPE-1" in response.get_json()["error"]["message"]


def test_another_partners_reference_is_404(
    client: FlaskClient, auth_b: dict[str, str], db: Engine
) -> None:
    disbursement(db, "FIN-79", partner="partner-a")
    response = client.get("/api/transactions/FIN-79", headers=auth_b)
    assert response.status_code == 404


@pytest.mark.parametrize("reference", ["bad ref", "x" * 41, "a/b"])
def test_malformed_reference_is_refused(
    client: FlaskClient, auth: dict[str, str], reference: str
) -> None:
    response = client.get(f"/api/transactions/{reference}", headers=auth)
    assert response.status_code in (404, 422)
    assert response.get_json()["error"]["code"] in ("VALIDATION_ERROR", "NOT_FOUND")


def test_bad_characters_get_422(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.get("/api/transactions/bad%20ref", headers=auth)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"


def test_needs_the_partner_key(client: FlaskClient) -> None:
    assert client.get("/api/transactions/FIN-1").status_code == 401


def test_request_log_line_names_the_reference(
    client: FlaskClient, auth: dict[str, str], db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    disbursement(db, "FIN-80")
    logger = logging.getLogger("integration_service")
    logger.addHandler(caplog.handler)
    try:
        client.get("/api/transactions/FIN-80", headers=auth)
    finally:
        logger.removeHandler(caplog.handler)
    lines = [r for r in caplog.records if r.getMessage() == "request"]
    fields = lines[-1].fields  # type: ignore[attr-defined]
    assert fields["external_reference"] == "FIN-80"
    assert fields["loan_reference"] == LOAN
