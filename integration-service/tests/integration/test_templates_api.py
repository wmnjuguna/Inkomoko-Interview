"""GET /api/clients/template, GET /api/loans/template and
GET /api/loans/{loan_reference}/template?step=... against the running Fineract, as the
least-privilege API user. Every default, range and option must be the one Fineract's own
template gives; no answer may show a Fineract id.

Fineract is shared and keeps its data: every test makes its own client and loan."""

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

from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.services.loans import fineract_from_settings, kigali_today

ID_KEY = re.compile(r"(^id$|_id$|Id$)")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GL_FILE = os.path.join(os.path.dirname(ROOT), "fineract/configuration/loan-product.json")
PRODUCT = "/loanproducts/external-id/PRD-SME-ASSET-FINANCE"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    password = os.environ.get("FINERACT_API_PASSWORD", "")
    if not password:
        pytest.skip("FINERACT_API_PASSWORD is not set (the API user's password from .env)")
    return replace(
        settings,
        fineract_username=os.environ.get("FINERACT_API_USERNAME", "integration-api"),
        fineract_password=password,
        gl_roles_config_path=GL_FILE,
        outbox_enabled=False,
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


def tag() -> str:
    return f"{secrets.token_hex(3)}-{secrets.randbelow(10**6)}"


def no_ids(value: Any) -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            assert not ID_KEY.search(key), f"id-like key {key} in a partner answer"
            no_ids(inner)
    elif isinstance(value, list):
        for inner in value:
            no_ids(inner)


def get(client: FlaskClient, auth: dict[str, str], url: str) -> Any:
    """GET; a 200 answer must not show a Fineract id (an error body has correlation_id)."""
    response = client.get(url, headers=auth)
    if response.status_code == 200:
        no_ids(response.get_json())
    return response


def field(body: dict[str, Any], name: str) -> dict[str, Any]:
    found = [f for f in body["fields"] if f["name"] == name]
    assert len(found) == 1, f"no field {name} in {body}"
    return found[0]


def new_client(client: FlaskClient, auth: dict[str, str]) -> str:
    uid = f"ITT-{secrets.randbelow(10**6):06d}"
    made = client.post(
        "/api/clients",
        json={
            "external_reference": f"IT-TCLI-{tag()}",
            "client_uid": uid,
            "first_name": "Tempa",
            "last_name": "Templatetest",
        },
        headers=auth,
    )
    assert made.status_code == 201, made.get_json()
    return uid


def new_loan(client: FlaskClient, auth: dict[str, str], uid: str, principal: int) -> str:
    applied = client.post(
        "/api/loans",
        json={"external_reference": f"IT-TAPP-{tag()}", "client_uid": uid, "principal": principal},
        headers=auth,
    )
    assert applied.status_code == 201, applied.get_json()
    return str(applied.get_json()["loan_reference"])


def post_step(client: FlaskClient, auth: dict[str, str], loan: str, action: str) -> None:
    response = client.post(
        f"/api/loans/{loan}/{action}", json={"external_reference": f"IT-T-{tag()}"}, headers=auth
    )
    assert response.status_code == 200, response.get_json()


# --- client form ------------------------------------------------------------------------------


def test_client_form_comes_from_fineract(client: FlaskClient, auth: dict[str, str]) -> None:
    response = get(client, auth, "/api/clients/template")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["form"] == "client"
    assert body["submit_to"] == "POST /api/clients"
    assert body["office"] == "Kigali Main Office"
    assert body["activation_date"] == kigali_today().isoformat()
    assert field(body, "client_uid")["pattern"] == "^[A-Z]{2,10}-[0-9]{6}$"
    assert field(body, "first_name")["required"] is True


def test_templates_need_the_api_key(client: FlaskClient) -> None:
    for url in ("/api/clients/template", "/api/loans/template"):
        response = client.get(url)
        assert response.status_code == 401
        assert response.get_json()["error"]["code"] == "UNAUTHENTICATED"


# --- loan application form --------------------------------------------------------------------


def test_loan_form_has_the_products_ranges(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    product = fineract.get(PRODUCT)
    uid = new_client(client, auth)
    response = get(client, auth, f"/api/loans/template?client_uid={uid}")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["form"] == "loan_application"
    assert body["client_uid"] == uid
    assert body["product"]["name"] == product["name"]
    assert body["product"]["short_name"] == product["shortName"]
    assert body["product"]["currency"]["code"] == product["currency"]["code"]
    principal = field(body, "principal")
    assert principal["default"] == int(product["principal"])
    assert principal["minimum"] == int(product["minPrincipal"])
    assert principal["maximum"] == int(product["maxPrincipal"])
    repayments = field(body, "number_of_repayments")
    assert repayments["minimum"] == product["minNumberOfRepayments"]
    assert repayments["maximum"] == product["maxNumberOfRepayments"]
    rate = field(body, "interest_rate_per_period")
    assert rate["default"] == product["interestRatePerPeriod"]
    assert field(body, "client_uid")["default"] == uid
    assert field(body, "expected_disbursement_date")["default"] == kigali_today().isoformat()
    assert response.headers.get("X-Correlation-ID")


def test_loan_form_without_a_client(client: FlaskClient, auth: dict[str, str]) -> None:
    response = get(client, auth, "/api/loans/template")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["client_uid"] is None


def test_loan_form_for_an_unknown_client_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = get(client, auth, "/api/loans/template?client_uid=NOPE-000000")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "CLIENT_NOT_FOUND"


def test_loan_form_with_a_bad_client_uid_is_422(client: FlaskClient, auth: dict[str, str]) -> None:
    response = get(client, auth, "/api/loans/template?client_uid=bad")
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"


# --- per-loan step forms ----------------------------------------------------------------------


def test_step_forms_follow_the_loan_through_its_life(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    uid = new_client(client, auth)
    loan = new_loan(client, auth, uid, 1_300_000)
    today = kigali_today().isoformat()

    approval = get(client, auth, f"/api/loans/{loan}/template?step=approval")
    assert approval.status_code == 200, approval.get_json()
    body = approval.get_json()
    assert body["form"] == "approval"
    assert body["loan_reference"] == loan
    assert body["client_uid"] == uid
    assert body["loan_status"] == "SUBMITTED_AND_PENDING_APPROVAL"
    assert body["available"] is True
    assert field(body, "approved_principal")["default"] == 1_300_000
    assert field(body, "approved_on")["default"] == today

    early = get(client, auth, f"/api/loans/{loan}/template?step=disbursement").get_json()
    assert early["available"] is False

    post_step(client, auth, loan, "approve")
    disbursement = get(client, auth, f"/api/loans/{loan}/template?step=disbursement").get_json()
    assert disbursement["available"] is True
    assert disbursement["loan_status"] == "APPROVED"
    assert field(disbursement, "amount")["default"] == 1_300_000
    assert field(disbursement, "transaction_date")["default"] == today

    post_step(client, auth, loan, "disburse")
    repayment = get(client, auth, f"/api/loans/{loan}/template?step=repayment").get_json()
    assert repayment["available"] is True
    own = fineract.get(
        f"/loans/external-id/{loan}/transactions/template", params={"command": "repayment"}
    )
    assert field(repayment, "amount")["default"] == round(own["amount"])
    assert repayment["values"]["next_due_amount"] == round(own["amount"])
    assert "Money Transfer" in [o["value"] for o in field(repayment, "payment_type")["options"]]

    payoff = get(client, auth, f"/api/loans/{loan}/template?step=payoff").get_json()
    assert payoff["available"] is True
    status = get(client, auth, f"/api/loans/{loan}").get_json()
    assert payoff["values"]["payoff_amount"] == status["payoff_amount"]
    assert payoff["values"]["outstanding_principal"] == 1_300_000


def test_unknown_loan_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = get(client, auth, "/api/loans/LN-1999-000001/template?step=approval")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "LOAN_NOT_FOUND"


@pytest.mark.parametrize("query", ["", "?step=writeoff"])
def test_a_missing_or_unknown_step_is_422(
    client: FlaskClient, auth: dict[str, str], query: str
) -> None:
    response = get(client, auth, f"/api/loans/LN-2026-000001/template{query}")
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
