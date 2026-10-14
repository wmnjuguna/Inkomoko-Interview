"""Part I, I-2 and I-3: requests fired at the same moment against the real app, the running
Fineract (as the least-privilege API user) and the running Mock ERP. Every request runs on
its own thread and test client, and one barrier releases them all together.

Each test counts all three systems, each from its own source:
  - Fineract: the loan's transactions, read back from Fineract;
  - our database: operations rows and outbox rows for the loan;
  - the ERP: journal entries for the loan, after the outbox has sent its rows.

The app runs with the outbox sender off, so each test sends the outbox with send_due()
itself. Every run uses fresh references. Fixtures and helpers come from test_disburse_api."""

# Fixtures imported from test_disburse_api are used as test arguments of the same name.
# ruff: noqa: F811

from __future__ import annotations

from collections import Counter
from functools import partial
from typing import Any

from flask.testing import FlaskClient
from flask_openapi3.openapi import OpenAPI
from sqlalchemy import Engine, select
from test_disburse_api import (  # noqa: F401 (fixtures are used by name)
    _clean,
    checked,
    disburse,
    erp,
    fineract,
    fineract_transactions,
    new_loan,
    settings,
    tag,
)
from test_money_concurrency import in_parallel, statuses

from integration_service.clients.erp import ErpClient, JournalSource
from integration_service.clients.errors import FineractNotFound
from integration_service.clients.fineract import FineractClient
from integration_service.models import operations, outbox
from integration_service.services.money import loan_path, transaction_path
from integration_service.services.outbox import send_due

PARALLEL = 10


# --- reads, one per system ------------------------------------------------------------------


def our_operations(db: Engine, loan: str, op_type: str) -> dict[str, str]:
    """Our operations rows for the loan: external_reference -> status. Asserts one row per
    reference (the table's own unique rule makes more impossible; this reads it back)."""
    with db.connect() as conn:
        rows = conn.execute(
            select(operations.c.external_reference, operations.c.status)
            .where(operations.c.loan_reference == loan)
            .where(operations.c.op_type == op_type)
        ).all()
    found = {row.external_reference: row.status for row in rows}
    assert len(found) == len(rows), rows
    return found


def our_outbox(db: Engine, loan: str, op_type: str) -> Counter[str]:
    """Outbox rows for the loan's operations of one type, counted per external_reference."""
    with db.connect() as conn:
        rows = conn.execute(
            select(operations.c.external_reference)
            .select_from(outbox.join(operations, outbox.c.operation_id == operations.c.id))
            .where(operations.c.loan_reference == loan)
            .where(operations.c.op_type == op_type)
        ).all()
    return Counter(row.external_reference for row in rows)


def erp_entries_for(erp: ErpClient, loan: str, source: JournalSource) -> list[dict[str, Any]]:
    """The ERP's entries for this loan and source. The search is a text match, so keep only
    the entries whose description names exactly this loan."""
    found = erp.search_journal_entries(q=loan, source=source, limit=200)
    return [e for e in found if loan in str(e.get("description", "")).split()]


def erp_lines(entry: dict[str, Any]) -> dict[tuple[str, str], int]:
    return {
        (line["account_code"], "DR" if line["debit"] else "CR"): int(
            line["debit"] or line["credit"]
        )
        for line in entry["lines"]
    }


def fineract_repayment_lines(txn: dict[str, Any]) -> dict[tuple[str, str], int]:
    """Part H from Fineract's own split: DR 1100 the amount, CR 1200 principal, CR 4100
    interest, CR 2100 overpayment. Zero lines left out. Nothing here comes from our answer."""
    lines = {
        ("1100", "DR"): round(txn["amount"]),
        ("1200", "CR"): round(txn.get("principalPortion") or 0),
        ("4100", "CR"): round(txn.get("interestPortion") or 0),
        ("2100", "CR"): round(txn.get("overpaymentPortion") or 0),
    }
    return {key: value for key, value in lines.items() if value}


def fineract_summary(fineract: FineractClient, loan: str) -> dict[str, int]:
    summary = fineract.get(loan_path(loan))["summary"]
    return {
        "principal": round(summary["principalOutstanding"]),
        "total": round(summary["totalOutstanding"]),
    }


def not_in_fineract(fineract: FineractClient, loan: str, op_key: str) -> bool:
    try:
        fineract.get(transaction_path(loan, op_key))
    except FineractNotFound:
        return True
    return False


# --- I-2a -----------------------------------------------------------------------------------


def test_i2a_ten_disbursements_with_one_reference_pay_out_once_everywhere(
    app: OpenAPI,
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-I2A-{tag()}"
    results = in_parallel(app, [lambda c: disburse(c, auth, loan, ref) for _ in range(PARALLEL)])

    # Answers: 200/201 with one body, or 202 while the first one still runs.
    counts = statuses(results)
    assert set(counts) <= {200, 201, 202}, counts
    done = [r.get_json() for r in results if r.status_code in (200, 201)]
    assert done, counts
    assert all(body == done[0] for body in done)
    for running in (r.get_json() for r in results if r.status_code == 202):
        assert running["external_reference"] == ref
        assert running["type"] == "DISBURSEMENT"

    # Fineract: one disbursement, under our op key.
    txns = fineract_transactions(fineract, loan, "disbursement")
    assert [t["externalId"] for t in txns] == [f"DSB:{ref}"]
    assert done[0]["amount"] == round(txns[0]["amount"])

    # Our database: one operation, one outbox row.
    assert our_operations(db, loan, "DSB") == {ref: "ERP_PENDING"}
    assert our_outbox(db, loan, "DSB") == Counter({ref: 1})

    # The ERP: one entry once the outbox has sent.
    send_due(db, erp)
    entries = erp_entries_for(erp, loan, "DISBURSEMENT")
    assert [e["external_reference"] for e in entries] == [f"DSB:{ref}"]
    assert entries[0]["total"] == round(txns[0]["amount"])
    assert our_operations(db, loan, "DSB") == {ref: "COMPLETED"}

    # A later copy gets the saved answer, and nothing new is written anywhere.
    assert disburse(client, auth, loan, ref).get_json() == done[0]
    assert len(fineract_transactions(fineract, loan, "disbursement")) == 1
    assert our_outbox(db, loan, "DSB") == Counter({ref: 1})


# --- I-2b -----------------------------------------------------------------------------------


def test_i2b_ten_disbursements_with_ten_references_pay_out_once_everywhere(
    app: OpenAPI,
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    loan = new_loan(client, auth)
    refs = [f"IT-I2B-{tag()}" for _ in range(PARALLEL)]
    results = in_parallel(app, [partial(disburse, auth=auth, loan=loan, ref=ref) for ref in refs])

    # Answers: one 200, nine 409 LOAN_ALREADY_DISBURSED (API.md, the documented refusal).
    counts = statuses(results)
    assert counts == Counter({200: 1, 409: PARALLEL - 1}), counts
    codes = {r.get_json()["error"]["code"] for r in results if r.status_code == 409}
    assert codes == {"LOAN_ALREADY_DISBURSED"}
    winner = next(ref for ref, r in zip(refs, results, strict=True) if r.status_code == 200)
    losers = [ref for ref in refs if ref != winner]

    # Fineract: one disbursement, the winner's; no transaction under any losing op key.
    txns = fineract_transactions(fineract, loan, "disbursement")
    assert [t["externalId"] for t in txns] == [f"DSB:{winner}"]
    assert all(not_in_fineract(fineract, loan, f"DSB:{ref}") for ref in losers)

    # Our database: the refusals were made before anything was saved (the one-live-
    # disbursement index, L2), so no losing reference has a row, and none reached Fineract.
    assert our_operations(db, loan, "DSB") == {winner: "ERP_PENDING"}
    assert our_outbox(db, loan, "DSB") == Counter({winner: 1})
    for ref in losers:
        assert checked(client.get(f"/api/transactions/{ref}", headers=auth)).status_code == 404

    # The ERP: one entry, the winner's.
    send_due(db, erp)
    entries = erp_entries_for(erp, loan, "DISBURSEMENT")
    assert [e["external_reference"] for e in entries] == [f"DSB:{winner}"]


# --- I-3 ------------------------------------------------------------------------------------

# Distinct references and their amounts, then extra copies of the first two.
I3_AMOUNTS = (15_000, 25_000, 35_000, 45_000, 55_000, 65_000)
I3_COPIES = (0, 0, 1, 1)


def test_i3_parallel_repayments_each_apply_once_with_fineracts_split(
    app: OpenAPI,
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    loan = new_loan(client, auth)
    assert disburse(client, auth, loan, f"IT-I3-DSB-{tag()}").status_code == 200
    refs = [f"IT-I3-RPY-{tag()}" for _ in I3_AMOUNTS]
    amounts = dict(zip(refs, I3_AMOUNTS, strict=True))
    plan = refs + [refs[i] for i in I3_COPIES]
    before = fineract_summary(fineract, loan)

    def repay(c: FlaskClient, ref: str) -> Any:
        return checked(
            c.post(
                f"/api/loans/{loan}/repayments",
                json={"external_reference": ref, "amount": amounts[ref]},
                headers=auth,
            )
        )

    results = in_parallel(app, [partial(repay, ref=ref) for ref in plan])
    counts = statuses(results)
    assert set(counts) <= {200, 202}, counts

    # Every reference has a 200, and its copies got the same body (or 202 while running).
    bodies: dict[str, list[Any]] = {ref: [] for ref in refs}
    for ref, result in zip(plan, results, strict=True):
        if result.status_code == 200:
            bodies[ref].append(result.get_json())
    for ref in refs:
        assert bodies[ref], f"{ref} never got a 200"
        assert all(body == bodies[ref][0] for body in bodies[ref]), ref

    # Fineract: exactly one repayment per distinct reference, of the amount sent.
    txns = {t["externalId"]: t for t in fineract_transactions(fineract, loan, "repayment")}
    assert sorted(txns) == sorted(f"RPY:{ref}" for ref in refs)
    for ref in refs:
        assert round(txns[f"RPY:{ref}"]["amount"]) == amounts[ref]

    # The balance: the initial outstanding minus what Fineract's own breakdown says was
    # applied, not the amounts this test sent.
    after = fineract_summary(fineract, loan)
    principal_applied = sum(round(t.get("principalPortion") or 0) for t in txns.values())
    owed_applied = sum(
        round(t.get(part) or 0)
        for t in txns.values()
        for part in (
            "principalPortion",
            "interestPortion",
            "feeChargesPortion",
            "penaltyChargesPortion",
        )
    )
    assert after["principal"] == before["principal"] - principal_applied
    assert after["total"] == before["total"] - owed_applied

    # Our database: one operation and one outbox row per distinct reference.
    assert set(our_operations(db, loan, "RPY")) == set(refs)
    assert our_outbox(db, loan, "RPY") == Counter(dict.fromkeys(refs, 1))

    # The ERP: one entry per reference, its lines equal to Fineract's split.
    send_due(db, erp)
    entries = erp_entries_for(erp, loan, "REPAYMENT")
    by_ref = Counter(e["external_reference"] for e in entries)
    assert by_ref == Counter({f"RPY:{ref}": 1 for ref in refs})
    for entry in entries:
        assert erp_lines(entry) == fineract_repayment_lines(txns[entry["external_reference"]])
    assert set(our_operations(db, loan, "RPY").values()) == {"COMPLETED"}

    # Every reference sent again gets its saved answer, which carries Fineract's split.
    for ref in refs:
        replay = repay(client, ref)
        assert replay.status_code == 200
        assert replay.get_json() == bodies[ref][0]
        txn = txns[f"RPY:{ref}"]
        assert replay.get_json()["principal_portion"] == round(txn["principalPortion"])
        assert replay.get_json()["interest_portion"] == round(txn["interestPortion"])
    assert len(fineract_transactions(fineract, loan, "repayment")) == len(refs)
