"""K-1: reconciliation, with all four outcomes set up on purpose on one fresh loan.

Run from the repo root, after setup (make up):

    make simulate-recon
    # or: uv run --project integration-service python scripts/simulate_recon.py [--in-process]

By default it calls the Integration Service on DEMO_SERVICE_URL (http://localhost:8091).
--in-process runs the service code from this checkout inside the script instead (a Flask test
client on the same integration_db, the Fineract API user and the ERP from .env). Use it while
the running container still has code without POST /api/reconciliation.

What it plants, all with fake data and fresh references:
  MATCHED              a disbursement and a repayment through our API, sent by the outbox
  AMOUNT_MISMATCH      an ERP entry posted straight to the ERP under RPY:<ref> with an
                       all-principal split, BEFORE the repayment <ref> is sent through our
                       API. Fineract's real split has interest, and our outbox cannot replace
                       the ERP entry (the ERP answers 409, so the outbox stops at
                       NEEDS_RECONCILIATION). No race with the outbox: the key is known first.
  MISSING_IN_ERP       a repayment posted straight into Fineract as the admin with a fake
                       externalId MANUAL-<run>, and one with no externalId
  MISSING_IN_FINERACT  an ERP entry posted straight to the ERP as RPY:<run>-GHOST

Then it calls POST /api/reconciliation twice: with the planted references (the report must
hold exactly the planted items) and with today's date range (the planted items must be there,
plus the no-externalId repayment). Other runs' entries in the shared ERP also show up in the
date-range report; only the planted items are checked.
Exit code 0 when every planted item has its expected outcome, 1 otherwise."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

from demo_gl_sync import ENV, erp, evidence, fineract
from demo_identity import RUN, Partner, Reply, history, reference, short
from demo_lifecycle import apply_and_approve, create_client, erp_entry, show_entry

from integration_service.services.loans import kigali_today
from integration_service.services.money import loan_path

PRINCIPAL = 1_000_000


class InProcessPartner(Partner):
    """The same calls, answered by the service code of this checkout, in this process."""

    def __init__(self) -> None:
        super().__init__()
        from integration_service.app import create_app
        from integration_service.config import Settings

        keys = dict(
            entry.split(":", 1) for entry in ENV.get("PARTNER_API_KEYS", "").split(",") if entry
        )
        settings = Settings(
            database_url=(
                f"postgresql+psycopg://{ENV.get('INTEGRATION_DB_USER', 'integration')}:"
                f"{ENV.get('INTEGRATION_DB_PASSWORD', '')}@localhost:5432/integration_db"
            ),
            partner_api_keys=keys,
            fineract_base_url=ENV.get(
                "DEMO_FINERACT_URL", "http://localhost:8080/fineract-provider/api/v1"
            ),
            fineract_username=ENV.get("FINERACT_API_USERNAME", "integration-api"),
            fineract_password=ENV.get("FINERACT_API_PASSWORD", ""),
            erp_base_url=ENV.get("DEMO_ERP_URL", "http://localhost:8090"),
            erp_api_key=ENV.get("ERP_API_KEY", ""),
            outbox_enabled=False,
        )
        self.app = create_app(settings).test_client()
        self.key = next(iter(keys.values()))

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Reply:
        self.calls += 1
        cid = f"{self.correlation_prefix()}{self.calls:03d}"
        response = self.app.open(
            path,
            method=method,
            json=body,
            headers={"X-API-Key": self.key, "X-Correlation-ID": cid},
        )
        return Reply(response.status_code, response.get_json(silent=True), cid)


@dataclass
class Planted:
    loan: str
    expected: dict[str, str]
    refs: dict[str, str]


def admin_repayment(loan: str, amount: int, external_id: str | None) -> None:
    body: dict[str, Any] = {
        "transactionDate": kigali_today().isoformat(),
        "transactionAmount": amount,
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }
    if external_id is not None:
        body["externalId"] = external_id
    admin = fineract()
    try:
        admin.post(f"{loan_path(loan)}/transactions", body, params={"command": "repayment"})
    finally:
        admin.close()


def erp_post(key: str, lines: list[tuple[str, int, int]]) -> None:
    client = erp()
    try:
        client.post_journal_entry(
            {
                "external_reference": key,
                "entry_date": kigali_today().isoformat(),
                "source": "REPAYMENT",
                "description": f"K-1 {RUN} planted straight in the ERP (fake data)",
                "lines": [
                    {"account_code": a, **({"debit": d} if d else {"credit": c})}
                    for a, d, c in lines
                ],
            }
        )
    finally:
        client.close()


def must(reply: Reply, status: int, what: str) -> Reply:
    if reply.status != status:
        raise SystemExit(f"{what} failed: {reply}")
    return reply


def plant(partner: Partner) -> Planted:
    uid = create_client(partner)
    loan, _, approved = apply_and_approve(partner, uid, PRINCIPAL)
    must(approved, 200, "approval")
    refs = {
        "dsb": reference("K1-DSB"),
        "rpy": reference("K1-RPY"),
        "mismatch": reference("K1-MIS"),
        "manual": f"MANUAL-{RUN}",
        "ghost": f"RPY:K1-{RUN}-GHOST",
    }
    must(
        partner.call("POST", f"/api/loans/{loan}/disburse", {"external_reference": refs["dsb"]}),
        200,
        "disbursement",
    )
    repay = f"/api/loans/{loan}/repayments"
    must(
        partner.call("POST", repay, {"external_reference": refs["rpy"], "amount": 100_000}),
        200,
        "repayment",
    )
    erp_post(f"RPY:{refs['mismatch']}", [("1100", 50_000, 0), ("1200", 0, 50_000)])
    must(
        partner.call("POST", repay, {"external_reference": refs["mismatch"], "amount": 50_000}),
        200,
        "mismatch repayment",
    )
    admin_repayment(loan, 30_000, refs["manual"])
    admin_repayment(loan, 20_000, None)
    erp_post(refs["ghost"], [("1100", 10_000, 0), ("1200", 0, 10_000)])

    must(partner.call("POST", "/admin/recover"), 200, "recovery pass")
    for key in (f"DSB:{refs['dsb']}", f"RPY:{refs['rpy']}"):
        if erp_entry(key) is None:
            raise SystemExit(f"the outbox did not send {key}")
    return Planted(
        loan,
        {
            f"DSB:{refs['dsb']}": "MATCHED",
            f"RPY:{refs['rpy']}": "MATCHED",
            f"RPY:{refs['mismatch']}": "AMOUNT_MISMATCH",
            refs["manual"]: "MISSING_IN_ERP",
            refs["ghost"]: "MISSING_IN_FINERACT",
        },
        refs,
    )


def fineract_state(loan: str) -> str:
    admin = fineract()
    try:
        data = admin.get(loan_path(loan), params={"associations": "transactions"})
    finally:
        admin.close()
    rows = []
    for t in data.get("transactions") or []:
        kind = str(t["type"]["code"]).rsplit(".", 1)[-1]
        rows.append(
            f"{kind} {t.get('externalId') or '(no externalId)'} {int(t['amount'])} "
            f"(principal {int(t.get('principalPortion') or 0)}, "
            f"interest {int(t.get('interestPortion') or 0)})"
        )
    return f"loan {loan}: " + "; ".join(rows)


def erp_state(keys: list[str]) -> str:
    return "; ".join(f"{k}: {show_entry(erp_entry(k, wait=0))}" for k in keys)


def check(items: list[dict[str, Any]], expected: dict[str, str], exact: bool) -> list[str]:
    found = {i["key"]: i["outcome"] for i in items}
    problems = [f"{k}: {found.get(k)} (want {v})" for k, v in expected.items() if found.get(k) != v]
    if exact and set(found) != set(expected):
        problems.append(f"extra items: {sorted(set(found) - set(expected), key=str)}")
    return problems


def show(body: dict[str, Any], keep: set[str | None] | None = None) -> None:
    print(json.dumps({k: body[k] for k in ("scope", "summary", "not_compared", "warnings")}))
    for item in body["items"]:
        if keep is not None and item["key"] not in keep:
            continue
        print(json.dumps(item))


def main() -> int:
    partner: Partner = InProcessPartner() if "--in-process" in sys.argv else Partner()
    print(f"K-1 run {RUN} ({'in-process' if '--in-process' in sys.argv else 'live service'})")
    planted = plant(partner)
    keys = list(planted.expected)
    today = kigali_today().isoformat()

    by_refs = partner.call("POST", "/api/reconciliation", {"references": keys})
    by_date = partner.call("POST", "/api/reconciliation", {"from": today, "to": today})
    problems: list[str] = []
    for reply in (by_refs, by_date):
        if reply.status != 200:
            problems.append(f"HTTP {reply.status}: {short(reply.body)}")
    if not problems:
        problems += check(by_refs.body["items"], planted.expected, exact=True)
        problems += check(by_date.body["items"], planted.expected, exact=False)
        no_key = [
            i
            for i in by_date.body["items"]
            if i["key"] is None and i["loan_reference"] == planted.loan
        ]
        if [i["outcome"] for i in no_key] != ["MISSING_IN_ERP"]:
            problems.append(f"no-externalId repayment: {no_key}")
    passed = not problems
    actual = (
        f"references: {by_refs.body.get('summary') if by_refs.status == 200 else by_refs}; "
        f"today: {by_date.body.get('summary') if by_date.status == 200 else by_date}"
    )
    ours = "; ".join(
        f"{r}: {history(partner, r)}"
        for r in (planted.refs["dsb"], planted.refs["rpy"], planted.refs["mismatch"])
    )
    evidence(
        "K-1 Reconciliation with all four outcomes set up on purpose",
        passed,
        f"fresh client and approved {PRINCIPAL} RWF loan {planted.loan}; planted "
        + ", ".join(f"{k} -> {v}" for k, v in planted.expected.items())
        + "; plus a direct Fineract repayment of 20000 with no externalId",
        f'POST /api/reconciliation {{"references": {keys}}}, then '
        f'{{"from": "{today}", "to": "{today}"}}',
        f"HTTP {by_refs.status} and HTTP {by_date.status} (reports below)",
        fineract_state(planted.loan),
        erp_state(keys),
        ours,
        "references: exactly the 5 planted items with their outcomes (2 MATCHED, 1 each of "
        "the others); today: the same 5 plus the no-externalId repayment as MISSING_IN_ERP",
        actual + ("" if passed else f"; problems: {problems}"),
        "each Fineract disbursement or repayment of a loan we know is matched to the ERP "
        "entry with the same key (its externalId). The ERP lines are compared with the lines "
        "the Part H mapping builds from Fineract's own split. Transactions made straight in "
        "Fineract have a foreign or no externalId, so the ERP has nothing under that key.",
    )
    print("\nReport, by references:")
    show(by_refs.body) if by_refs.status == 200 else print(by_refs)
    print("\nReport, today (planted items and the no-externalId one only):")
    if by_date.status == 200:
        show(by_date.body, {*keys, None})
    partner.close()
    print(f"\nResult: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
