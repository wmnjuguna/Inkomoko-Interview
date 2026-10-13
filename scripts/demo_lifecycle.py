"""D-1: one loan from application to Closed (obligations met).

Run from the repo root, after setup (make up), with the Integration Service's outbox on:

    make demo-lifecycle
    # or: uv run --project integration-service python scripts/demo_lifecycle.py

5,000,000 RWF on the SME Asset Finance product (18% a year, 12 monthly installments):
application, approval, disbursement, 3 partial repayments, then a payoff of exactly the
amount Fineract quotes (payoff_amount in GET /api/loans/{loan_reference}, taken from
Fineract's own prepayLoan figure). The loan must end Closed (obligations met).

Every request goes to the Integration Service with the partner key, like a partner. After
every stage it prints:
  - the loan as the service reports it (GET /api/loans/{loan_reference}),
  - the loan as Fineract holds it (admin GET, read only): status and summary balances,
  - Fineract's own GL entries for the loan, added up per account (admin GET, read only),
  - the ERP's journal entries for the loan, added up per account (GET, read only).
After each money stage it waits (up to DEMO_ERP_WAIT seconds, default 60) for the outbox to
send the journal entry, then checks that the ERP lines are Fineract's split and that the ERP
totals per account equal Fineract's GL totals.

Fresh references each run, fake data ("Test Client One"), nothing is deleted.
Exit code 0 when every stage matched what should happen, 1 otherwise."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import httpx
from demo_gl_sync import ENV, evidence
from demo_identity import (
    RUN,
    Partner,
    Reply,
    admin_get,
    free_client_uid,
    history,
    reference,
    short,
)

PRINCIPAL = 5_000_000
PARTIAL_REPAYMENTS = (500_000, 750_000, 1_000_000)
ERP_URL = ENV.get("DEMO_ERP_URL", "http://localhost:8090")
ERP_WAIT = float(ENV.get("DEMO_ERP_WAIT", "60"))

Totals = dict[str, tuple[int, int]]  # account code -> (debits, credits)


# --- reads (shared with simulate_duplicates.py) --------------------------------------------


def erp_entries(**params: Any) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{ERP_URL}/api/journal-entries",
        params=params,
        headers={"X-API-Key": ENV.get("ERP_API_KEY", "change-me-erp-key")},
        timeout=10.0,
    )
    response.raise_for_status()
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def erp_entry(external_reference: str, wait: float = ERP_WAIT) -> dict[str, Any] | None:
    """The ERP entry with this reference, waiting for the outbox to send it."""
    deadline = time.monotonic() + wait
    while True:
        found = erp_entries(reference=external_reference)
        if found or time.monotonic() >= deadline:
            return found[0] if found else None
        time.sleep(1.0)


def erp_totals(loan_reference: str) -> tuple[int, Totals]:
    """How many ERP entries name this loan, and their debits and credits per account."""
    entries = erp_entries(q=loan_reference)
    debit: Counter[str] = Counter()
    credit: Counter[str] = Counter()
    for entry in entries:
        for line in entry["lines"]:
            debit[line["account_code"]] += int(line["debit"])
            credit[line["account_code"]] += int(line["credit"])
    return len(entries), {code: (debit[code], credit[code]) for code in sorted(debit | credit)}


def fineract_loan(loan_reference: str) -> dict[str, Any]:
    status, data = admin_get(f"/loans/external-id/{loan_reference}")
    if status != 200 or not isinstance(data, dict):
        raise SystemExit(f"Fineract has no loan {loan_reference} (HTTP {status})")
    return data


def fineract_view(loan: dict[str, Any]) -> str:
    """Fineract's own status and balances. Internal ids are not printed."""
    summary = loan.get("summary") or {}
    keys = ("principalDisbursed", "principalPaid", "principalOutstanding", "interestCharged")
    keys += ("interestPaid", "interestOutstanding", "totalOutstanding", "totalOverpaid")
    balances = ", ".join(f"{k} {int(summary[k])}" for k in keys if summary.get(k) is not None)
    status = loan["status"]
    flags = [k for k in ("closedObligationsMet", "overpaid") if status.get(k)]
    return (
        f"status {status['id']} '{status['value']}'{' ' + str(flags) if flags else ''}; "
        f"{balances or 'no summary yet (not disbursed)'}"
    )


def fineract_gl(loan: dict[str, Any]) -> Totals:
    """Fineract's own GL entries for the loan, per account (reversed entries left out)."""
    status, data = admin_get("/journalentries", {"loanId": str(loan["id"]), "limit": "-1"})
    if status != 200 or not isinstance(data, dict):
        raise SystemExit(f"could not read Fineract's journal entries (HTTP {status})")
    debit: Counter[str] = Counter()
    credit: Counter[str] = Counter()
    for item in data["pageItems"]:
        if item.get("reversed"):
            continue
        side = debit if item["entryType"]["value"] == "DEBIT" else credit
        side[item["glAccountCode"]] += round(item["amount"])
    return {code: (debit[code], credit[code]) for code in sorted(debit | credit)}


def show_totals(totals: Totals) -> str:
    if not totals:
        return "none"
    return ", ".join(f"{code} DR {d:,} CR {c:,}" for code, (d, c) in totals.items())


def show_entry(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "not in the ERP (yet)"
    lines = " / ".join(
        f"{'DR' if line['debit'] else 'CR'} {line['account_code']} "
        f"{line['debit'] or line['credit']:,}"
        for line in entry["lines"]
    )
    return f"{entry['external_reference']} {entry['source']} {entry['entry_date']}: {lines}"


def entry_lines(entry: dict[str, Any] | None) -> dict[tuple[str, str], int]:
    if entry is None:
        return {}
    return {
        (line["account_code"], "DR" if line["debit"] else "CR"): int(
            line["debit"] or line["credit"]
        )
        for line in entry["lines"]
    }


def expected_repayment_lines(answer: dict[str, Any]) -> dict[tuple[str, str], int]:
    """Part H: DR 1100 the amount / CR 1200 principal / CR 4100 interest / CR 2100
    overpayment, with Fineract's split from the answer. Zero lines are left out."""
    lines = {
        ("1100", "DR"): answer["amount"],
        ("1200", "CR"): answer["principal_portion"],
        ("4100", "CR"): answer["interest_portion"],
        ("2100", "CR"): answer["overpayment_portion"],
    }
    return {key: value for key, value in lines.items() if value}


def service_view(partner: Partner, loan_reference: str) -> tuple[Reply, str]:
    reply = partner.call("GET", f"/api/loans/{loan_reference}")
    if reply.status != 200:
        return reply, str(reply)
    loan = reply.body
    balances = loan.get("balances") or {}
    keys = ("principal_outstanding", "interest_outstanding", "total_repaid", "total_outstanding")
    shown = ", ".join(f"{k} {balances[k]}" for k in keys if balances.get(k) is not None)
    return reply, (
        f"GET /api/loans: status {loan['status']['code']}, closed_obligations_met "
        f"{loan['closed_obligations_met']}, payoff_amount {loan['payoff_amount']}"
        f"{'; ' + shown if shown else ''}"
    )


def create_client(partner: Partner) -> str:
    uid = free_client_uid(partner)
    reply = partner.call(
        "POST",
        "/api/clients",
        {
            "external_reference": reference("CLI"),
            "client_uid": uid,
            "first_name": "Test",
            "last_name": "Client One",
        },
    )
    if reply.status != 201:
        raise SystemExit(f"could not create the client: {reply}")
    return uid


def apply_and_approve(partner: Partner, uid: str, principal: int) -> tuple[str, Reply, Reply]:
    applied = partner.call(
        "POST",
        "/api/loans",
        {"external_reference": reference("APP"), "client_uid": uid, "principal": principal},
    )
    if applied.status != 201:
        raise SystemExit(f"the loan application failed: {applied}")
    loan_reference = str(applied.body["loan_reference"])
    approved = partner.call(
        "POST", f"/api/loans/{loan_reference}/approve", {"external_reference": reference("APR")}
    )
    return loan_reference, applied, approved


# --- D-1 ----------------------------------------------------------------------------------


class Stage:
    """Prints one stage in the evidence template, with the state of all three systems."""

    def __init__(self, partner: Partner, loan_reference: str) -> None:
        self.partner = partner
        self.loan_reference = loan_reference
        self.results: list[bool] = []

    def snapshot(self) -> tuple[Reply, str, str, Totals, int, Totals]:
        service, service_text = service_view(self.partner, self.loan_reference)
        loan = fineract_loan(self.loan_reference)
        gl = fineract_gl(loan)
        count, erp = erp_totals(self.loan_reference)
        return service, service_text, fineract_view(loan), gl, count, erp

    def report(
        self,
        title: str,
        before: str,
        request: str,
        reply: Reply,
        expected: str,
        checks: dict[str, bool],
        why: str,
        external_reference: str,
    ) -> Reply:
        service, service_text, fineract_text, gl, count, erp = self.snapshot()
        failed = [name for name, ok in checks.items() if not ok]
        self.results.append(
            evidence(
                title,
                not failed,
                before,
                request,
                str(reply),
                f"{fineract_text}; GL entries for the loan: {show_totals(gl)}",
                f"{count} entries for {self.loan_reference}; per account: {show_totals(erp)}",
                f"{service_text}; {external_reference}: "
                f"{history(self.partner, external_reference)}",
                expected,
                "all checks held" if not failed else f"FAILED checks: {failed}",
                why,
            )
        )
        return service


def main() -> int:
    partner = Partner()
    print(f"D-1 run {RUN}")
    uid = create_client(partner)
    applied = partner.call(
        "POST",
        "/api/loans",
        {"external_reference": reference("APP"), "client_uid": uid, "principal": PRINCIPAL},
    )
    if applied.status != 201:
        print(f"the loan application failed: {applied}")
        return 1
    loan_reference = str(applied.body["loan_reference"])
    print(f"client_uid {uid}, loan_reference {loan_reference}")
    stage = Stage(partner, loan_reference)

    # 1. Application.
    loan = fineract_loan(loan_reference)
    stage.report(
        "D-1.1 Loan application",
        f"client {uid} is active; it has no loan yet",
        f"POST /api/loans {{client_uid {uid}, principal {PRINCIPAL:,}}}",
        applied,
        "201 with a loan_reference; Fineract status 100 'Submitted and pending approval'",
        {
            "201": applied.status == 201,
            "status in the answer": applied.body.get("status") == "SUBMITTED_AND_PENDING_APPROVAL",
            "principal": applied.body.get("principal") == PRINCIPAL,
            "Fineract status 100": loan["status"]["id"] == 100,
        },
        "the service issues LN-<year>-<n> from a Postgres sequence and uses it as the loan "
        "externalId in Fineract; the term and rate come from the product",
        applied.body["external_reference"],
    )

    approved = partner.call(
        "POST", f"/api/loans/{loan_reference}/approve", {"external_reference": reference("APR")}
    )

    # 2. Approval.
    loan = fineract_loan(loan_reference)
    stage.report(
        "D-1.2 Approval",
        "the loan is submitted and pending approval",
        f"POST /api/loans/{loan_reference}/approve",
        approved,
        "200 APPROVED for the full principal; Fineract status 200 'Approved'; no money moved, "
        "so no GL and no ERP entries yet",
        {
            "200": approved.status == 200,
            "approved principal": approved.body.get("approved_principal") == PRINCIPAL,
            "Fineract status 200": loan["status"]["id"] == 200,
            "no GL entries": fineract_gl(loan) == {},
            "no ERP entries": erp_totals(loan_reference)[0] == 0,
        },
        "approval is a Fineract command on the loan's externalId; it moves no money",
        approved.body["external_reference"],
    )

    # 3. Disbursement.
    ref = reference("DSB")
    before = fineract_view(fineract_loan(loan_reference))
    paid = partner.call(
        "POST", f"/api/loans/{loan_reference}/disburse", {"external_reference": ref}
    )
    entry = erp_entry(f"DSB:{ref}")
    loan = fineract_loan(loan_reference)
    stage.report(
        "D-1.3 Disbursement",
        before,
        f"POST /api/loans/{loan_reference}/disburse {{external_reference {ref}}}",
        paid,
        f"200, {PRINCIPAL:,} paid out, loan ACTIVE; the ERP gets DSB:{ref} = DR 1200 / CR 1100 "
        f"{PRINCIPAL:,}, the same as Fineract's own GL entries",
        {
            "200": paid.status == 200,
            "amount": paid.body.get("amount") == PRINCIPAL,
            "Fineract ACTIVE": loan["status"]["id"] == 300,
            "ERP entry": entry_lines(entry)
            == {("1200", "DR"): PRINCIPAL, ("1100", "CR"): PRINCIPAL},
            "ERP totals = Fineract GL": erp_totals(loan_reference)[1] == fineract_gl(loan),
        },
        "Fineract pays out under the per-loan lock with transaction externalId DSB:<reference>; "
        "the service reads that transaction back and builds the journal entry only from it, "
        "in the same DB transaction that marks the request done; the outbox sends it. "
        f"ERP entry: {show_entry(entry)}",
        ref,
    )

    # 4-6. Three partial repayments.
    for n, amount in enumerate(PARTIAL_REPAYMENTS, start=1):
        ref = reference(f"RPY{n}")
        loan = fineract_loan(loan_reference)
        owed_before = int(loan["summary"]["principalOutstanding"])
        before = fineract_view(loan)
        paid = partner.call(
            "POST",
            f"/api/loans/{loan_reference}/repayments",
            {"external_reference": ref, "amount": amount},
        )
        entry = erp_entry(f"RPY:{ref}")
        loan = fineract_loan(loan_reference)
        answer = paid.body if paid.status == 200 else {}
        split = (
            f"principal {answer.get('principal_portion')}, interest "
            f"{answer.get('interest_portion')}, overpayment {answer.get('overpayment_portion')}"
        )
        stage.report(
            f"D-1.{3 + n} Partial repayment {n} of {len(PARTIAL_REPAYMENTS)}",
            before,
            f"POST /api/loans/{loan_reference}/repayments {{external_reference {ref}, "
            f"amount {amount:,}}}",
            paid,
            "200 with Fineract's split; loan still ACTIVE; principal outstanding drops by the "
            "principal part; the ERP entry is DR 1100 amount / CR 1200 principal / CR 4100 "
            "interest, the same as Fineract's GL",
            {
                "200": paid.status == 200,
                "amount": answer.get("amount") == amount,
                "split adds up": answer.get("principal_portion", 0)
                + answer.get("interest_portion", 0)
                + answer.get("overpayment_portion", 0)
                == amount,
                "Fineract ACTIVE": loan["status"]["id"] == 300,
                "principal outstanding": int(loan["summary"]["principalOutstanding"])
                == owed_before - answer.get("principal_portion", 0),
                "ERP entry = split": entry is not None
                and entry_lines(entry) == expected_repayment_lines(answer),
                "ERP totals = Fineract GL": erp_totals(loan_reference)[1] == fineract_gl(loan),
            },
            f"Fineract split the {amount:,} ({split}); the entry is built from that split. "
            f"ERP entry: {show_entry(entry)}",
            ref,
        )

    # 7. Payoff with Fineract's own figure.
    quote, quote_text = service_view(partner, loan_reference)
    payoff = quote.body.get("payoff_amount") if quote.status == 200 else None
    ref = reference("RPYOFF")
    paid = partner.call(
        "POST",
        f"/api/loans/{loan_reference}/repayments",
        {"external_reference": ref, "amount": payoff},
    )
    entry = erp_entry(f"RPY:{ref}")
    loan = fineract_loan(loan_reference)
    answer = paid.body if paid.status == 200 else {}
    service = stage.report(
        "D-1.7 Payoff with Fineract's payoff figure",
        quote_text,
        f"POST /api/loans/{loan_reference}/repayments {{external_reference {ref}, amount "
        f"{payoff} (payoff_amount from GET /api/loans)}}",
        paid,
        "200; Fineract status 600 'Closed (obligations met)', total outstanding 0, no "
        "overpayment; the ERP entry is Fineract's split",
        {
            "payoff quoted": isinstance(payoff, int) and payoff > 0,
            "200": paid.status == 200,
            "no overpayment": answer.get("overpayment_portion") == 0,
            "Fineract 600": loan["status"]["id"] == 600,
            "closedObligationsMet": bool(loan["status"].get("closedObligationsMet")),
            "nothing outstanding": int(loan["summary"]["totalOutstanding"]) == 0,
            "ERP entry = split": entry is not None
            and entry_lines(entry) == expected_repayment_lines(answer),
            "ERP totals = Fineract GL": erp_totals(loan_reference)[1] == fineract_gl(loan),
        },
        "payoff_amount is Fineract's own prepayLoan figure for today, so paying exactly it "
        f"meets every obligation. ERP entry: {show_entry(entry)}",
        ref,
    )
    if service.status != 200 or service.body["status"]["code"] != "CLOSED_OBLIGATIONS_MET":
        stage.results.append(False)
        print(f"\nGET /api/loans does not show CLOSED_OBLIGATIONS_MET: {short(service.body)}")

    # Summary across the whole loan.
    count, erp = erp_totals(loan_reference)
    gl = fineract_gl(fineract_loan(loan_reference))
    expected_count = 2 + len(PARTIAL_REPAYMENTS)
    print("\n=== D-1 summary ===")
    print(f"Fineract GL for {loan_reference}: {show_totals(gl)}")
    print(f"ERP entries for {loan_reference}: {count}; per account: {show_totals(erp)}")
    print(
        f"ERP entries expected: {expected_count} (1 disbursement + "
        f"{len(PARTIAL_REPAYMENTS) + 1} repayments); ERP = Fineract GL: {erp == gl}"
    )
    stage.results.append(count == expected_count and erp == gl)

    partner.close()
    print(f"\n{sum(stage.results)} of {len(stage.results)} checks matched")
    return 0 if all(stage.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
