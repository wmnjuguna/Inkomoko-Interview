"""POST /api/loans, POST /api/loans/{loan_reference}/approve and GET /api/loans/{...}
against the running Fineract, as the least-privilege API user.

Fineract is shared and keeps its data: every test makes its own client and references, and
the loan_reference sequence starts at a random number each test (the test database is new
for each run, but Fineract still holds the loans of earlier runs). Nothing is deleted."""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine, text

from integration_service.clients.errors import FineractTimeout
from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.errors import ApiError, ErrorCode
from integration_service.schemas.loans import ApprovalRequest, LoanApplicationRequest
from integration_service.services.loans import (
    apply_for_loan,
    approve_loan,
    fineract_from_settings,
    kigali_today,
    recover_application,
    recover_approval,
)
from integration_service.services.operations import get_operation

ID_KEY = re.compile(r"(^id$|_id$|Id$)")
LOAN_REFERENCE = re.compile(r"^LN-\d{4}-\d{6}$")


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
    start = 100000 + secrets.randbelow(890000)
    with db.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE outbox, operation_events, loans, clients, operations "
                "RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text(f"ALTER SEQUENCE loan_reference_seq RESTART WITH {start}"))


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


def tag() -> str:
    return f"{secrets.token_hex(3)}-{secrets.randbelow(10**6)}"


def track(response: Any) -> Any:
    no_ids(response.get_json())
    return response


@pytest.fixture
def client_uid(client: FlaskClient, auth: dict[str, str]) -> str:
    uid = f"ITL-{secrets.randbelow(10**6):06d}"
    response = client.post(
        "/api/clients",
        json={
            "external_reference": f"IT-LCLI-{tag()}",
            "client_uid": uid,
            "first_name": "Loana",
            "last_name": "Testborrower",
        },
        headers=auth,
    )
    assert response.status_code == 201, response.get_json()
    return uid


def apply(
    client: FlaskClient, auth: dict[str, str], uid: str, ref: str, principal: Any = 1200000
) -> Any:
    return track(
        client.post(
            "/api/loans",
            json={"external_reference": ref, "client_uid": uid, "principal": principal},
            headers=auth,
        )
    )


def approve(client: FlaskClient, auth: dict[str, str], loan: str, ref: str) -> Any:
    return track(
        client.post(f"/api/loans/{loan}/approve", json={"external_reference": ref}, headers=auth)
    )


def get_loan(client: FlaskClient, auth: dict[str, str], loan: str) -> Any:
    return track(client.get(f"/api/loans/{loan}", headers=auth))


# --- application -------------------------------------------------------------------------------


def test_apply_then_replay_returns_the_same_loan_reference(
    client: FlaskClient, auth: dict[str, str], client_uid: str
) -> None:
    ref = f"IT-APP-{tag()}"
    first = apply(client, auth, client_uid, ref)
    assert first.status_code == 201
    data = first.get_json()
    assert LOAN_REFERENCE.match(data["loan_reference"])
    assert data["loan_reference"].startswith(f"LN-{kigali_today().year}-")
    assert data == {
        "loan_reference": data["loan_reference"],
        "client_uid": client_uid,
        "principal": 1200000,
        "status": "SUBMITTED_AND_PENDING_APPROVAL",
        "expected_disbursement_date": kigali_today().isoformat(),
        "external_reference": ref,
    }

    again = apply(client, auth, client_uid, ref)
    assert again.status_code == 200
    assert again.get_json() == data

    other = apply(client, auth, client_uid, ref, principal=1300000)
    assert other.status_code == 409
    assert other.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    loan = get_loan(client, auth, data["loan_reference"])
    assert loan.status_code == 200
    body = loan.get_json()
    assert body["status"] == {
        "code": "SUBMITTED_AND_PENDING_APPROVAL",
        "value": body["status"]["value"],
    }
    assert body["client_uid"] == client_uid
    assert body["principal"] == 1200000
    assert body["balances"] is None
    assert body["payoff_amount"] is None


def test_apply_for_an_unknown_client_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = apply(client, auth, "ITL-999999", f"IT-APP-{tag()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "CLIENT_NOT_FOUND"


@pytest.mark.parametrize("principal", [1000.6, "1200000", 0, -1])
def test_principal_must_be_whole_positive_rwf(
    client: FlaskClient, auth: dict[str, str], principal: Any
) -> None:
    response = apply(client, auth, "ITL-000001", f"IT-APP-{tag()}", principal=principal)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"


def test_principal_outside_the_product_range_is_422_and_failed(
    client: FlaskClient, auth: dict[str, str], client_uid: str
) -> None:
    ref = f"IT-APP-{tag()}"
    response = apply(client, auth, client_uid, ref, principal=50000)
    assert response.status_code == 422
    assert "between 100000 and 50000000" in response.get_json()["error"]["message"]
    op = track(client.get(f"/api/transactions/{ref}", headers=auth)).get_json()["operations"][0]
    assert op["status"] == "FAILED"


def test_past_expected_disbursement_date_is_422(
    client: FlaskClient, auth: dict[str, str], client_uid: str
) -> None:
    yesterday = (kigali_today() - timedelta(days=1)).isoformat()
    response = track(
        client.post(
            "/api/loans",
            json={
                "external_reference": f"IT-APP-{tag()}",
                "client_uid": client_uid,
                "principal": 1200000,
                "expected_disbursement_date": yesterday,
            },
            headers=auth,
        )
    )
    assert response.status_code == 422


def test_a_replay_after_the_expected_date_has_passed_gets_the_saved_answer(
    db: Engine, fineract: FineractClient, client_uid: str
) -> None:
    """Part F: same reference and same data after it finished gives 200 and the saved body,
    even when the request's date is in the past by now (a retry after midnight)."""
    today = kigali_today()
    request = LoanApplicationRequest(
        external_reference=f"IT-APP-{tag()}",
        client_uid=client_uid,
        principal=1200000,
        expected_disbursement_date=today,
    )
    first = apply_for_loan(db, fineract, request, "partner-a", None, today=today)
    assert first.status == 201
    tomorrow = today + timedelta(days=1)
    again = apply_for_loan(db, fineract, request, "partner-a", None, today=tomorrow)
    assert (again.status, again.body) == (200, first.body)

    # A new reference with a past date is still refused, and nothing is saved.
    late = request.model_copy(update={"external_reference": f"IT-APP-{tag()}"})
    with pytest.raises(ApiError) as caught:
        apply_for_loan(db, fineract, late, "partner-a", None, today=tomorrow)
    assert caught.value.status == 422
    assert get_operation(db, "APP", late.external_reference) is None


def test_a_loan_reference_fineract_already_holds_is_skipped(
    client: FlaskClient, auth: dict[str, str], client_uid: str, db: Engine
) -> None:
    taken = apply(client, auth, client_uid, f"IT-APP-{tag()}").get_json()["loan_reference"]
    # As if integration_db were rebuilt: the sequence gives that number again.
    with db.begin() as conn:
        conn.execute(text(f"ALTER SEQUENCE loan_reference_seq RESTART WITH {int(taken[-6:])}"))

    ref = f"IT-APP-{tag()}"
    response = apply(client, auth, client_uid, ref)

    assert response.status_code == 201
    loan_reference = response.get_json()["loan_reference"]
    assert loan_reference != taken
    op = get_operation(db, "APP", ref)
    assert op is not None
    assert op.status == "COMPLETED"
    assert op.attempts == 2
    assert op.loan_reference == loan_reference


# --- approval ----------------------------------------------------------------------------------


def test_approve_once_replay_and_a_second_reference_is_409(
    client: FlaskClient, auth: dict[str, str], client_uid: str
) -> None:
    loan = apply(client, auth, client_uid, f"IT-APP-{tag()}").get_json()["loan_reference"]
    ref = f"IT-APR-{tag()}"

    first = approve(client, auth, loan, ref)
    assert first.status_code == 200
    assert first.get_json() == {
        "loan_reference": loan,
        "client_uid": client_uid,
        "status": "APPROVED",
        "approved_on": kigali_today().isoformat(),
        "approved_principal": 1200000,
        "external_reference": ref,
    }

    again = approve(client, auth, loan, ref)
    assert again.status_code == 200
    assert again.get_json() == first.get_json()

    second = approve(client, auth, loan, f"IT-APR-{tag()}")
    assert second.status_code == 409
    assert second.get_json()["error"]["code"] == "LOAN_ALREADY_APPROVED"

    body = get_loan(client, auth, loan).get_json()
    assert body["status"] == {"code": "APPROVED", "value": "Approved"}
    assert body["approved_principal"] == 1200000
    assert body["approved_on"] == kigali_today().isoformat()


def test_approved_outside_the_service_is_409_and_failed(
    client: FlaskClient, auth: dict[str, str], client_uid: str, admin: FineractClient
) -> None:
    loan = apply(client, auth, client_uid, f"IT-APP-{tag()}").get_json()["loan_reference"]
    today = kigali_today().isoformat()
    admin.post(
        f"/loans/external-id/{loan}",
        {"approvedOnDate": today, "dateFormat": "yyyy-MM-dd", "locale": "en"},
        params={"command": "approve"},
    )
    ref = f"IT-APR-{tag()}"

    response = approve(client, auth, loan, ref)

    assert response.status_code == 409
    error = response.get_json()["error"]
    assert error["code"] == "LOAN_ALREADY_APPROVED"
    op = track(client.get(f"/api/transactions/{ref}", headers=auth)).get_json()["operations"][0]
    assert op["status"] == "FAILED"


def test_unknown_loan_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    assert get_loan(client, auth, "LN-1999-000001").status_code == 404
    response = approve(client, auth, "LN-1999-000001", f"IT-APR-{tag()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "LOAN_NOT_FOUND"


def test_malformed_loan_reference_is_422(client: FlaskClient, auth: dict[str, str]) -> None:
    assert client.get("/api/loans/7", headers=auth).status_code == 422


def test_future_approval_date_is_422(
    client: FlaskClient, auth: dict[str, str], client_uid: str
) -> None:
    loan = apply(client, auth, client_uid, f"IT-APP-{tag()}").get_json()["loan_reference"]
    tomorrow = (kigali_today() + timedelta(days=1)).isoformat()
    response = track(
        client.post(
            f"/api/loans/{loan}/approve",
            json={"external_reference": f"IT-APR-{tag()}", "approved_on": tomorrow},
            headers=auth,
        )
    )
    assert response.status_code == 422


# --- unclear outcomes ----------------------------------------------------------------------------


class LostAnswer:
    """The real Fineract, but the answer to every write is lost after it lands.
    With blind_lookups, loan lookups fail too."""

    def __init__(self, real: FineractClient, blind_lookups: bool = False) -> None:
        self.real = real
        self.blind = blind_lookups
        self.writes = 0

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        if self.blind and path.startswith("/loans/external-id/"):
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
        self.writes += 1
        self.real.post(path, body, idempotency_key, params, correlation_id)
        raise FineractTimeout("answer lost")


def test_lost_application_answer_is_found_by_lookup(
    db: Engine, fineract: FineractClient, client_uid: str
) -> None:
    ref = f"IT-APP-{tag()}"
    lost = LostAnswer(fineract)
    request = LoanApplicationRequest(
        external_reference=ref, client_uid=client_uid, principal=900000
    )

    answer = apply_for_loan(db, lost, request, "partner-a", "c-lost")

    assert answer.status == 201
    assert lost.writes == 1
    op = get_operation(db, "APP", ref)
    assert op is not None and op.status == "COMPLETED"
    found = fineract.get(f"/loans/external-id/{answer.body['loan_reference']}")
    assert found["clientExternalId"] == client_uid


def test_lost_approval_answer_is_found_by_lookup(
    db: Engine, fineract: FineractClient, client: FlaskClient, auth: dict[str, str], client_uid: str
) -> None:
    loan = apply(client, auth, client_uid, f"IT-APP-{tag()}").get_json()["loan_reference"]
    ref = f"IT-APR-{tag()}"
    lost = LostAnswer(fineract)

    answer = approve_loan(db, lost, loan, ApprovalRequest(external_reference=ref), "partner-a", "c")

    assert answer.status == 200
    assert answer.body["status"] == "APPROVED"
    assert lost.writes == 1


def test_unclear_application_and_approval_are_finished_by_recovery(
    db: Engine, fineract: FineractClient, client: FlaskClient, auth: dict[str, str], client_uid: str
) -> None:
    ref = f"IT-APP-{tag()}"
    blind = LostAnswer(fineract, blind_lookups=True)
    request = LoanApplicationRequest(
        external_reference=ref, client_uid=client_uid, principal=800000
    )
    with pytest.raises(ApiError) as caught:
        apply_for_loan(db, blind, request, "partner-a", "c-blind")
    assert caught.value.code == ErrorCode.FINERACT_TIMEOUT
    op = get_operation(db, "APP", ref)
    assert op is not None and op.status == "FINERACT_PENDING" and op.loan_reference

    repeat = apply(client, auth, client_uid, ref, principal=800000)
    assert repeat.status_code == 202
    assert blind.writes == 1

    assert recover_application(db, fineract, op) == "done"
    replay = apply(client, auth, client_uid, ref, principal=800000)
    assert replay.status_code == 200
    assert replay.get_json()["loan_reference"] == op.loan_reference

    apr_ref = f"IT-APR-{tag()}"
    with pytest.raises(ApiError):
        approve_loan(
            db,
            blind,
            op.loan_reference,
            ApprovalRequest(external_reference=apr_ref),
            "partner-a",
            "c",
        )
    apr = get_operation(db, "APR", apr_ref)
    assert apr is not None and apr.status == "FINERACT_PENDING"
    assert recover_approval(db, fineract, apr) == "done"
    assert get_loan(client, auth, op.loan_reference).get_json()["status"]["code"] == "APPROVED"


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
