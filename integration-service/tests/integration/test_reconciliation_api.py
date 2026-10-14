"""POST /api/reconciliation against the running Fineract (as the least-privilege API user) and
the running Mock ERP. Every outcome is set up on purpose on one loan of our own:

- MATCHED: a disbursement through our API, its outbox sent with send_due().
- AMOUNT_MISMATCH: a journal entry posted straight to the ERP under RPY:<ref> with the wrong
  split BEFORE the repayment is sent through our API under <ref>. Fineract then holds the real
  transaction under the same key, and our outbox (never sent here) cannot overwrite the ERP.
- IN_FLIGHT: a repayment through our API whose outbox row is not sent yet.
- MISSING_IN_ERP: repayments posted straight into Fineract as the admin, one with a fake
  externalId and one with none.
- MISSING_IN_FINERACT: an entry posted straight to the ERP with an op-key reference that no
  Fineract transaction has.

The ERP and Fineract are shared, so a date-range report also holds other runs' entries; the
tests check the planted items only. Fake data, nothing is deleted."""

# ruff: noqa: F811

from __future__ import annotations

from typing import Any

from flask.testing import FlaskClient
from sqlalchemy import Engine
from test_disburse_api import (  # noqa: F401 (fixtures are used by name)
    _clean,
    admin,
    checked,
    disburse,
    erp,
    fineract,
    new_loan,
    settings,
    tag,
)

from integration_service.clients.erp import ErpClient
from integration_service.clients.fineract import FineractClient
from integration_service.services.loans import kigali_today
from integration_service.services.money import loan_path
from integration_service.services.outbox import send_due

URL = "/api/reconciliation"


def repay(client: FlaskClient, auth: dict[str, str], loan: str, ref: str, amount: int) -> None:
    answer = client.post(
        f"/api/loans/{loan}/repayments",
        json={"external_reference": ref, "amount": amount},
        headers=auth,
    )
    assert answer.status_code == 200, answer.get_json()


def admin_repayment(admin: FineractClient, loan: str, amount: int, external_id: str | None) -> None:
    body: dict[str, Any] = {
        "transactionDate": kigali_today().isoformat(),
        "transactionAmount": amount,
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }
    if external_id is not None:
        body["externalId"] = external_id
    admin.post(f"{loan_path(loan)}/transactions", body, params={"command": "repayment"})


def erp_post(erp: ErpClient, reference: str, lines: list[tuple[str, int, int]]) -> None:
    erp.post_journal_entry(
        {
            "external_reference": reference,
            "entry_date": kigali_today().isoformat(),
            "source": "REPAYMENT",
            "description": "reconciliation test, fake data",
            "lines": [
                {"account_code": a, **({"debit": d} if d else {"credit": c})} for a, d, c in lines
            ],
        }
    )


def plant(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    admin: FineractClient,
    erp: ErpClient,
) -> dict[str, Any]:
    t = tag()
    loan = new_loan(client, auth)
    paid = disburse(client, auth, loan, f"RC-DSB-{t}")
    assert paid.status_code == 200, paid.get_json()
    send_due(db, erp)

    mismatch = f"RC-MIS-{t}"
    erp_post(erp, f"RPY:{mismatch}", [("1100", 50_000, 0), ("1200", 0, 50_000)])
    repay(client, auth, loan, mismatch, 50_000)
    repay(client, auth, loan, f"RC-FLY-{t}", 40_000)

    admin_repayment(admin, loan, 30_000, f"MANUAL-{t}")
    admin_repayment(admin, loan, 20_000, None)
    erp_post(erp, f"RPY:RC-GHOST-{t}", [("1100", 10_000, 0), ("1200", 0, 10_000)])
    return {
        "loan": loan,
        "expected": {
            f"DSB:RC-DSB-{t}": "MATCHED",
            f"RPY:{mismatch}": "AMOUNT_MISMATCH",
            f"RPY:RC-FLY-{t}": "IN_FLIGHT",
            f"MANUAL-{t}": "MISSING_IN_ERP",
            f"RPY:RC-GHOST-{t}": "MISSING_IN_FINERACT",
        },
    }


def outcomes(body: dict[str, Any]) -> dict[str | None, str]:
    return {i["key"]: i["outcome"] for i in body["items"]}


def test_date_range_report_has_every_planted_outcome(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    admin: FineractClient,
    erp: ErpClient,
) -> None:
    planted = plant(client, auth, db, admin, erp)
    today = kigali_today().isoformat()
    response = checked(client.post(URL, json={"from": today, "to": today}, headers=auth))
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    found = outcomes(body)
    for key, outcome in planted["expected"].items():
        assert found.get(key) == outcome, (key, found.get(key))

    loan_items = [i for i in body["items"] if i["loan_reference"] == planted["loan"]]
    no_key = [i for i in loan_items if i["key"] is None]
    assert len(no_key) == 1 and no_key[0]["outcome"] == "MISSING_IN_ERP"
    assert no_key[0]["fineract"]["amount"] == 20_000

    mismatch = next(
        i
        for i in body["items"]
        if i["outcome"] == "AMOUNT_MISMATCH" and i["loan_reference"] == planted["loan"]
    )
    assert mismatch["erp"]["total"] == 50_000
    assert mismatch["reasons"], mismatch
    split = mismatch["fineract"]
    assert split["principal_portion"] + split["interest_portion"] == 50_000
    assert split["interest_portion"] > 0  # so the ERP's all-principal split is wrong

    assert body["summary"]["total"] == len(body["items"])
    assert body["compared_types"] == ["DISBURSEMENT", "REPAYMENT"]
    assert body["not_compared"]["fineract_types"].get("ACCRUAL", 0) >= 0


def test_reference_report_holds_only_the_named_items(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    admin: FineractClient,
    erp: ErpClient,
) -> None:
    planted = plant(client, auth, db, admin, erp)
    keys = list(planted["expected"])
    response = checked(client.post(URL, json={"references": keys}, headers=auth))
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert outcomes(body) == planted["expected"]
    assert body["summary"] == {
        "MATCHED": 1,
        "MISSING_IN_ERP": 1,
        "MISSING_IN_FINERACT": 1,
        "AMOUNT_MISMATCH": 1,
        "IN_FLIGHT": 1,
        "total": 5,
    }
    assert body["scope"] == {"from": None, "to": None, "references": keys}


def test_bad_scope_is_422_with_our_error_format(client: FlaskClient, auth: dict[str, str]) -> None:
    response = client.post(
        URL,
        json={"from": "2026-01-01", "to": "2026-12-31"},
        headers={**auth, "X-Correlation-ID": "c-rc-1"},
    )
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["correlation_id"] == "c-rc-1"
    assert error["details"]["problems"][0]["field"] == "to"


def test_needs_a_partner_key(client: FlaskClient) -> None:
    response = client.post(URL, json={"references": ["X"]})
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "UNAUTHENTICATED"


def test_is_in_the_openapi_spec(client: FlaskClient) -> None:
    spec = client.get("/openapi/openapi.json").get_json()
    assert "post" in spec["paths"][URL]
