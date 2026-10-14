"""I-3: repayments of one loan fired at the same moment, with different references, plus
copies of some of them.

Run from the repo root, after setup (make up), with the Integration Service's outbox on:

    make simulate-concurrent-repay
    # or: uv run --project integration-service python scripts/simulate_concurrent_repay.py

It makes a fresh client and a 1,000,000 RWF loan, approves and disburses it, then fires 10
repayments together (one thread and connection each, one barrier): 6 references of different
amounts, plus 2 more copies of the first reference and 2 more of the second.

Then it checks, each from its own source:
  - Fineract has exactly one repayment per reference, of the amount sent;
  - Fineract's final outstanding equals the initial outstanding minus what Fineract's own
    transaction breakdowns say was applied (not the amounts this script sent):
      principalOutstanding after = before - sum(principalPortion)
      totalOutstanding after     = before - sum(principal + interest + fee + penalty portions)
  - our database has one operations row and one outbox row per reference;
  - the ERP has one entry per reference, its lines equal to Fineract's split
    (DR 1100 amount / CR 1200 principal / CR 4100 interest / CR 2100 overpayment);
  - every copy of a reference got the same saved answer (or 202 while it was running).

Fresh references each run, fake data, nothing is deleted.
Exit code 0 when all of that holds, 1 otherwise."""

from __future__ import annotations

from collections import Counter
from typing import Any

from demo_gl_sync import evidence
from demo_identity import RUN, Partner, Reply, admin_get, history, reference, short
from demo_lifecycle import erp_entry, fineract_loan, fineract_view, show_entry
from simulate_concurrent_disburse import (
    burst,
    count_rows,
    loan_entries,
    new_approved_loan,
    our_rows,
    tally,
)

AMOUNTS = (15_000, 25_000, 35_000, 45_000, 55_000, 65_000)
COPIES = (0, 0, 1, 1)  # extra copies of the first and second reference
OWED_PARTS = ("principalPortion", "interestPortion", "feeChargesPortion", "penaltyChargesPortion")


def repayments(loan_reference: str) -> dict[str, dict[str, Any]]:
    """Fineract's repayment transactions on the loan (not reversed), by externalId."""
    status, data = admin_get(
        f"/loans/external-id/{loan_reference}", {"associations": "transactions"}
    )
    if status != 200 or not isinstance(data, dict):
        raise SystemExit(f"could not read the loan's transactions (HTTP {status})")
    return {
        str(t.get("externalId")): t
        for t in data.get("transactions") or []
        if t["type"].get("repayment") and not t.get("manuallyReversed")
    }


def balances(loan_reference: str) -> tuple[int, int]:
    summary = fineract_loan(loan_reference)["summary"]
    return round(summary["principalOutstanding"]), round(summary["totalOutstanding"])


def fineract_lines(txn: dict[str, Any]) -> dict[tuple[str, str], int]:
    lines = {
        ("1100", "DR"): round(txn["amount"]),
        ("1200", "CR"): round(txn.get("principalPortion") or 0),
        ("4100", "CR"): round(txn.get("interestPortion") or 0),
        ("2100", "CR"): round(txn.get("overpaymentPortion") or 0),
    }
    return {key: value for key, value in lines.items() if value}


def erp_lines(entry: dict[str, Any] | None) -> dict[tuple[str, str], int]:
    if entry is None:
        return {}
    return {
        (line["account_code"], "DR" if line["debit"] else "CR"): int(
            line["debit"] or line["credit"]
        )
        for line in entry["lines"]
    }


def split(txn: dict[str, Any]) -> str:
    return (
        f"amount {round(txn['amount']):,} = principal {round(txn.get('principalPortion') or 0):,}"
        f" + interest {round(txn.get('interestPortion') or 0):,}"
        f" + overpayment {round(txn.get('overpaymentPortion') or 0):,}"
    )


def main() -> int:
    partner = Partner()
    loan_reference = new_approved_loan(partner)
    paid_out = partner.call(
        "POST",
        f"/api/loans/{loan_reference}/disburse",
        {"external_reference": reference("I3-DSB")},
    )
    if paid_out.status != 200:
        print(f"disbursement failed: {paid_out}")
        return 1
    refs = [reference("I3-RPY") for _ in AMOUNTS]
    amounts = dict(zip(refs, AMOUNTS, strict=True))
    plan = refs + [refs[i] for i in COPIES]
    path = f"/api/loans/{loan_reference}/repayments"
    print(f"I-3 run {RUN}: loan {loan_reference}, {len(refs)} references, {len(plan)} calls")

    principal_before, total_before = balances(loan_reference)
    before = (
        f"Fineract: {fineract_view(fineract_loan(loan_reference))}; repayments "
        f"{len(repayments(loan_reference))}; ERP REPAYMENT entries "
        f"{len(loan_entries(loan_reference, 'REPAYMENT'))}; ours: {our_rows(loan_reference, 'RPY')}"
    )

    def call(ref: str) -> Any:
        return lambda p: p.call("POST", path, {"external_reference": ref, "amount": amounts[ref]})

    replies: list[Reply] = burst([call(ref) for ref in plan])
    during = f"right after the burst: {our_rows(loan_reference, 'RPY')}"

    # Answers: each reference has a 200, its copies the same body or 202.
    answers: dict[str, list[Reply]] = {ref: [] for ref in refs}
    for ref, reply in zip(plan, replies, strict=True):
        answers[ref].append(reply)
    answers_ok = all(r.status in (200, 202) for r in replies) and all(
        any(r.status == 200 for r in got)
        and len({short(r.body) for r in got if r.status == 200}) == 1
        for got in answers.values()
    )

    # The ERP, once the outbox has sent every entry.
    entries = {ref: erp_entry(f"RPY:{ref}") for ref in refs}
    erp_refs = Counter(e["external_reference"] for e in loan_entries(loan_reference, "REPAYMENT"))

    # Fineract.
    txns = repayments(loan_reference)
    once_each = sorted(txns) == sorted(f"RPY:{ref}" for ref in refs) and all(
        round(txns[f"RPY:{ref}"]["amount"]) == amounts[ref] for ref in refs
    )
    principal_after, total_after = balances(loan_reference)
    principal_applied = sum(round(t.get("principalPortion") or 0) for t in txns.values())
    owed_applied = sum(round(t.get(part) or 0) for t in txns.values() for part in OWED_PARTS)
    balance_ok = (
        principal_after == principal_before - principal_applied
        and total_after == total_before - owed_applied
    )

    # Lines per reference: the ERP's against Fineract's split.
    lines_ok = once_each and all(
        erp_lines(entries[ref]) == fineract_lines(txns[f"RPY:{ref}"]) for ref in refs
    )
    erp_ok = erp_refs == Counter({f"RPY:{ref}": 1 for ref in refs})

    # Ours.
    operations = count_rows(loan_reference, "RPY")
    box = count_rows(loan_reference, "RPY", "outbox")
    ours_ok = operations == str(len(refs)) and box == str(len(refs))

    # Copies sent again later still get the saved answer.
    replays = {
        ref: partner.call("POST", path, {"external_reference": ref, "amount": amounts[ref]})
        for ref in refs[:2]
    }
    replay_ok = all(
        r.status == 200 and any(a.status == 200 and a.body == r.body for a in answers[ref])
        for ref, r in replays.items()
    )
    after_replay = len(repayments(loan_reference))

    passed = (
        answers_ok
        and once_each
        and balance_ok
        and lines_ok
        and erp_ok
        and ours_ok
        and replay_ok
        and after_replay == len(refs)
    )
    per_ref = "; ".join(
        f"{ref} sent {amounts[ref]:,} x{plan.count(ref)}: "
        + ", ".join(str(r.status) for r in answers[ref])
        for ref in refs
    )
    fineract_text = "; ".join(
        f"RPY:{ref} {split(txns[f'RPY:{ref}'])}" for ref in refs if f"RPY:{ref}" in txns
    )
    erp_text = "; ".join(show_entry(entries[ref]) for ref in refs)
    counts = (
        f"Requests sent: {len(plan)} ({len(refs)} references); Fineract repayments: "
        f"{len(txns)}; Integration operations: {operations}; outbox rows: {box}; ERP journal "
        f"entries: {sum(erp_refs.values())}; principal outstanding {principal_before:,} -> "
        f"{principal_after:,} (Fineract applied {principal_applied:,}); total outstanding "
        f"{total_before:,} -> {total_after:,} (Fineract applied {owed_applied:,}); checks: "
        f"answers {answers_ok}, once each {once_each}, balance {balance_ok}, ERP lines = "
        f"Fineract split {lines_ok}, one ERP entry each {erp_ok}, our rows {ours_ok}, replay "
        f"{replay_ok}"
    )
    evidence(
        f"I-3 {len(plan)} repayments at the same time: {len(refs)} references plus copies",
        passed,
        before + f"; principal outstanding {principal_before:,}, total {total_before:,}",
        f"POST {path}, {len(plan)} at once (one barrier): {per_ref}",
        f"{tally(replies)}; first answer of each reference: "
        + "; ".join(
            short(next((r.body for r in answers[ref] if r.status == 200), None), 200)
            for ref in refs[:2]
        )
        + " ...",
        f"{fineract_view(fineract_loan(loan_reference))}; {len(txns)} repayments: {fineract_text}",
        f"{sum(erp_refs.values())} REPAYMENT entries for {loan_reference}: {erp_text}",
        f"{during}; after the outbox: {our_rows(loan_reference, 'RPY')}; {refs[0]}: "
        f"{history(partner, refs[0])}",
        "one Fineract repayment, one operations row, one outbox row and one ERP entry per "
        "reference; copies get the same answer; outstanding after = before minus Fineract's "
        "own applied portions; ERP lines = Fineract's split",
        counts,
        "each reference is its own operation (L1 keeps copies to one run: 202 while it runs, "
        "then the saved answer). The per-loan lock (L3) sends the different references to "
        "Fineract one at a time, so Fineract applies each in turn and none fails on a "
        "concurrent update. The split in the answer and in the ERP entry is read back from "
        "Fineract's transaction by its externalId, never computed by us.",
    )
    partner.close()
    print(f"\n{counts}")
    print(f"\nResult: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
