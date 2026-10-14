"""I-2: 10 disbursements of one loan fired at the same moment.

Run from the repo root, after setup (make up), with the Integration Service's outbox on:

    make simulate-concurrent-disburse
    # or: uv run --project integration-service python scripts/simulate_concurrent_disburse.py

Two cases, each on its own fresh client and approved 1,000,000 RWF loan:

  I-2a  10 requests with the SAME external_reference
  I-2b  10 requests, each with its OWN external_reference

Each request runs on its own thread with its own HTTP connection, and one barrier releases
all 10 together. Then it counts, each from its own source:

  Fineract disbursements    disbursement transactions on the loan (fineract_default, read only)
  Fineract commands         commands Fineract ran with Idempotency-Key DSB:<reference>:<attempt>
  Integration operations    operations rows of type DSB for the loan (integration_db, read only)
  Outbox rows               outbox rows for those operations (integration_db, read only)
  ERP journal entries       ERP entries for the loan with source DISBURSEMENT (ERP search)

State is printed before the burst, right after it (before the outbox has sent: the
"during" view of our database and the ERP), and after the ERP has the entry.

Expected:
  I-2a  every answer 200 with one body (or 202 while the first still runs); Fineract 1,
        commands 1, operations 1, outbox 1, ERP 1.
  I-2b  one 200 and nine 409 LOAN_ALREADY_DISBURSED; Fineract 1 (the winner's), no command
        and no operations row for any losing reference, outbox 1, ERP 1.

Fresh references each run, fake data, nothing is deleted.
Exit code 0 when both cases hold, 1 otherwise."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from demo_gl_sync import evidence, sql
from demo_identity import INTEGRATION_DB, RUN, Partner, Reply, history, reference, short
from demo_lifecycle import (
    apply_and_approve,
    create_client,
    erp_entries,
    erp_entry,
    fineract_loan,
    fineract_view,
    show_entry,
)
from simulate_duplicates import erp_disbursements, fineract_commands, fineract_disbursements

PARALLEL = 10
PRINCIPAL = 1_000_000


# --- the burst (shared with simulate_concurrent_repay.py) -----------------------------------


def burst(calls: list[Callable[[Partner], Reply]]) -> list[Reply]:
    """Runs each call on its own thread and connection; one barrier releases them all."""
    barrier = threading.Barrier(len(calls))
    replies: list[Reply | None] = [None] * len(calls)
    partners = [Partner(f"{RUN}{n:02d}") for n in range(len(calls))]

    def run(n: int) -> None:
        barrier.wait()
        replies[n] = calls[n](partners[n])

    threads = [threading.Thread(target=run, args=(n,)) for n in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(180)
    for partner in partners:
        partner.close()
    missing = [n for n, reply in enumerate(replies) if reply is None]
    if missing:
        raise SystemExit(f"calls {missing} gave no answer")
    return [reply for reply in replies if reply is not None]


def tally(replies: list[Reply]) -> str:
    counts = Counter(f"{r.status}{' ' + str(r.code()) if r.code() else ''}" for r in replies)
    return ", ".join(f"{n} x HTTP {what}" for what, n in sorted(counts.items()))


def our_rows(loan_reference: str, op_type: str) -> str:
    """Our operations rows for the loan, as reference=status, and their outbox rows."""
    ops = sql(
        INTEGRATION_DB,
        "SELECT coalesce(string_agg(external_reference || '=' || status, ', ' "
        "ORDER BY external_reference), 'none') FROM operations "
        "WHERE loan_reference = :'loan' AND op_type = :'type'",
        loan=loan_reference,
        type=op_type,
    )
    box = sql(
        INTEGRATION_DB,
        "SELECT count(*) || ' (' || coalesce(string_agg(DISTINCT x.status, ', '), 'none') "
        "|| ')' FROM outbox x JOIN operations o ON o.id = x.operation_id "
        "WHERE o.loan_reference = :'loan' AND o.op_type = :'type'",
        loan=loan_reference,
        type=op_type,
    )
    return f"operations rows {op_type}: {ops}; outbox rows: {box}"


def count_rows(loan_reference: str, op_type: str, table: str = "operations") -> str:
    if table == "outbox":
        return sql(
            INTEGRATION_DB,
            "SELECT count(*) FROM outbox x JOIN operations o ON o.id = x.operation_id "
            "WHERE o.loan_reference = :'loan' AND o.op_type = :'type'",
            loan=loan_reference,
            type=op_type,
        )
    return sql(
        INTEGRATION_DB,
        "SELECT count(*) FROM operations WHERE loan_reference = :'loan' AND op_type = :'type'",
        loan=loan_reference,
        type=op_type,
    )


def rows_for(external_reference: str) -> str:
    return sql(
        INTEGRATION_DB,
        "SELECT count(*) FROM operations WHERE external_reference = :'ref'",
        ref=external_reference,
    )


def loan_entries(loan_reference: str, source: str) -> list[dict[str, Any]]:
    """ERP entries for exactly this loan (the search is a text match)."""
    found = erp_entries(q=loan_reference, source=source, limit=200)
    return [e for e in found if loan_reference in str(e.get("description", "")).split()]


def state(loan_reference: str) -> str:
    return (
        f"Fineract: {fineract_view(fineract_loan(loan_reference))}, disbursements "
        f"{fineract_disbursements(loan_reference)}; ERP DISBURSEMENT entries "
        f"{erp_disbursements(loan_reference)}; ours: {our_rows(loan_reference, 'DSB')}"
    )


def new_approved_loan(partner: Partner) -> str:
    uid = create_client(partner)
    loan_reference, _, approved = apply_and_approve(partner, uid, PRINCIPAL)
    if approved.status != 200:
        raise SystemExit(f"approval failed: {approved}")
    return loan_reference


# --- I-2a -----------------------------------------------------------------------------------


def same_reference(partner: Partner) -> bool:
    loan_reference = new_approved_loan(partner)
    ref = reference("I2A-DSB")
    body = {"external_reference": ref}
    path = f"/api/loans/{loan_reference}/disburse"
    print(f"\nI-2a run {RUN}: loan_reference {loan_reference}, reference {ref}")
    before = state(loan_reference)

    replies = burst([lambda p: p.call("POST", path, body) for _ in range(PARALLEL)])
    during = f"right after the burst: {our_rows(loan_reference, 'DSB')}"

    entry = erp_entry(f"DSB:{ref}")
    in_fineract = fineract_disbursements(loan_reference)
    commands = fineract_commands(f"DSB:{ref}")
    operations = count_rows(loan_reference, "DSB")
    box = count_rows(loan_reference, "DSB", "outbox")
    in_erp = len(loan_entries(loan_reference, "DISBURSEMENT"))
    done = [r for r in replies if r.status in (200, 201)]
    running = [r for r in replies if r.status == 202]
    same = bool(done) and all(r.body == done[0].body for r in done)
    passed = (
        len(done) + len(running) == PARALLEL
        and same
        and all(r.body.get("external_reference") == ref for r in running)
        and in_fineract == "1"
        and commands == "1"
        and operations == "1"
        and box == "1"
        and in_erp == 1
    )
    counts = (
        f"Requests sent: {PARALLEL}; Fineract disbursements: {in_fineract}; Fineract commands "
        f"for DSB:{ref}: {commands}; Integration operations: {operations}; outbox rows: {box}; "
        f"ERP journal entries: {in_erp}"
    )
    evidence(
        f"I-2a {PARALLEL} disbursements at the same time, same reference",
        passed,
        before,
        f"POST {path} {short(body)}, {PARALLEL} at once (one barrier)",
        f"{tally(replies)}; the 200 bodies are identical: {same}; first: "
        f"{short(done[0].body) if done else 'none'}",
        f"{fineract_view(fineract_loan(loan_reference))}; disbursement transactions: "
        f"{in_fineract}; commands run with Idempotency-Key DSB:{ref}:<attempt>: {commands}",
        f"{in_erp} DISBURSEMENT entry for {loan_reference}: {show_entry(entry)}",
        f"{during}; after the outbox: {our_rows(loan_reference, 'DSB')}; {history(partner, ref)}",
        "every answer 200 with one body, or 202 while running; Fineract 1, commands 1, "
        "operations 1, outbox 1, ERP 1",
        counts,
        "the first insert of (DSB, reference) wins (ON CONFLICT DO NOTHING on the unique "
        "key, L1); the other 9 find that row: 202 while it runs, its saved answer once it is "
        "done. Only the winner calls Fineract, and it writes one outbox row in the same "
        "transaction as FINERACT_DONE, so the ERP gets one entry.",
    )
    print(f"Result I-2a: {'PASS' if passed else 'FAIL'}")
    return passed


# --- I-2b -----------------------------------------------------------------------------------


def different_references(partner: Partner) -> bool:
    loan_reference = new_approved_loan(partner)
    refs = [reference("I2B-DSB") for _ in range(PARALLEL)]
    path = f"/api/loans/{loan_reference}/disburse"
    print(f"\nI-2b run {RUN}: loan_reference {loan_reference}, references {refs[0]} ... {refs[-1]}")
    before = state(loan_reference)

    def call(ref: str) -> Callable[[Partner], Reply]:
        return lambda p: p.call("POST", path, {"external_reference": ref})

    replies = burst([call(ref) for ref in refs])
    during = f"right after the burst: {our_rows(loan_reference, 'DSB')}"

    winners = [ref for ref, r in zip(refs, replies, strict=True) if r.status == 200]
    refused = [r for r in replies if r.status == 409]
    codes = {r.code() for r in refused}
    winner = winners[0] if len(winners) == 1 else None
    entry = erp_entry(f"DSB:{winner}") if winner else None
    losers = [ref for ref in refs if ref != winner]
    in_fineract = fineract_disbursements(loan_reference)
    winner_commands = fineract_commands(f"DSB:{winner}") if winner else "-"
    loser_commands = sum(int(fineract_commands(f"DSB:{ref}")) for ref in losers)
    loser_rows = sum(int(rows_for(ref)) for ref in losers)
    operations = count_rows(loan_reference, "DSB")
    box = count_rows(loan_reference, "DSB", "outbox")
    erp_refs = [e["external_reference"] for e in loan_entries(loan_reference, "DISBURSEMENT")]
    passed = (
        winner is not None
        and len(refused) == PARALLEL - 1
        and codes == {"LOAN_ALREADY_DISBURSED"}
        and in_fineract == "1"
        and winner_commands == "1"
        and loser_commands == 0
        and loser_rows == 0
        and operations == "1"
        and box == "1"
        and erp_refs == [f"DSB:{winner}"]
    )
    counts = (
        f"Requests sent: {PARALLEL}; Fineract disbursements: {in_fineract}; Fineract commands "
        f"for the winner: {winner_commands}, for the 9 others: {loser_commands}; Integration "
        f"operations: {operations} (rows for the 9 others: {loser_rows}); outbox rows: {box}; "
        f"ERP journal entries: {len(erp_refs)}"
    )
    first_refusal = short(refused[0].body) if refused else "none"
    evidence(
        f"I-2b {PARALLEL} disbursements at the same time, different references",
        passed,
        before,
        f"POST {path}, {PARALLEL} at once (one barrier), each with its own external_reference",
        f"{tally(replies)}; winner {winner}; a refusal: {first_refusal}",
        f"{fineract_view(fineract_loan(loan_reference))}; disbursement transactions: "
        f"{in_fineract}; commands run for the winner: {winner_commands}, for the 9 others: "
        f"{loser_commands}",
        f"{len(erp_refs)} DISBURSEMENT entry for {loan_reference}: {show_entry(entry)}",
        f"{during}; after the outbox: {our_rows(loan_reference, 'DSB')}; operations rows for "
        f"the 9 refused references: {loser_rows}"
        + (f"; winner: {history(partner, winner)}" if winner else ""),
        "one 200, nine 409 LOAN_ALREADY_DISBURSED; Fineract 1; no command and no row for any "
        "refused reference; outbox 1; ERP 1",
        counts,
        "each reference is new, so L1 does not stop them. The partial unique index "
        "operations_one_live_disbursement (L2) lets one live DSB row exist per loan: the 9 "
        "other inserts fail, nothing is saved for them, Fineract is never called, and they "
        "get 409. Behind it: the per-loan lock (L3), Fineract's unique transaction externalId "
        "and Idempotency-Key (L4), and Fineract's loan state, the last line of defence (L5).",
    )
    print(f"Result I-2b: {'PASS' if passed else 'FAIL'}")
    return passed


def main() -> int:
    partner = Partner()
    results = [same_reference(partner), different_references(partner)]
    partner.close()
    passed = all(results)
    print(f"\nResult: {'PASS' if passed else 'FAIL'} (I-2a {results[0]}, I-2b {results[1]})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
