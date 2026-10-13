"""I-1: the same disbursement sent 4 times, one after another.

Run from the repo root, after setup (make up), with the Integration Service's outbox on:

    make simulate-duplicates
    # or: uv run --project integration-service python scripts/simulate_duplicates.py

It makes a fresh client and an approved 1,000,000 RWF loan through the Integration Service,
then sends POST /api/loans/{loan_reference}/disburse 4 times in a row with the same body
(same external_reference). Then it counts, each from its own source:

  Requests received         the service's request log lines for those 4 calls (docker logs,
                            matched by the correlation ids this script sent)
  Fineract disbursements    disbursement transactions on the loan (fineract_default, read only)
  ERP journal entries       ERP entries for the loan with source DISBURSEMENT (ERP search)
  Integration transactions  operations rows of type DSB for the loan (integration_db, read only)

It also counts the commands Fineract ran with this operation's Idempotency-Key
(DSB:<reference>:<attempt>, in m_portfolio_command_source), which must be 1.

Expected: 4, 1, 1, 1. The first call must answer 200 and the other 3 must answer 200 with
exactly the same body. Fresh references each run, fake data, nothing is deleted.
Exit code 0 when all of that holds, 1 otherwise."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from demo_gl_sync import REPO, evidence, sql
from demo_identity import (
    INTEGRATION_DB,
    RUN,
    Partner,
    history,
    operations_count,
    reference,
    short,
)
from demo_lifecycle import (
    apply_and_approve,
    create_client,
    erp_entries,
    erp_entry,
    fineract_loan,
    fineract_view,
    show_entry,
)

TIMES = 4
PRINCIPAL = 1_000_000


def fineract_disbursements(loan_reference: str) -> str:
    return sql(
        "fineract_default",
        "SELECT count(*) FROM m_loan_transaction t JOIN m_loan l ON l.id = t.loan_id "
        "WHERE l.external_id = :'loan' AND t.transaction_type_enum = 1 AND NOT t.is_reversed",
        loan=loan_reference,
    )


def fineract_commands(op_key: str) -> str:
    """Commands Fineract ran with an Idempotency-Key of this operation (<op key>:<attempt>)."""
    return sql(
        "fineract_default",
        "SELECT count(*) FROM m_portfolio_command_source WHERE idempotency_key LIKE :'keys'",
        keys=f"{op_key}:%",
    )


def erp_disbursements(loan_reference: str) -> int:
    return len(erp_entries(q=loan_reference, source="DISBURSEMENT"))


def requests_logged(since: str, correlation_ids: list[str]) -> int:
    """The service's request log lines (one JSON line per request) for these calls."""
    result = subprocess.run(  # noqa: S603 (fixed argument list)
        [  # noqa: S607
            "docker",
            "compose",
            "-f",
            str(REPO / "docker-compose.yml"),
            "logs",
            "--no-log-prefix",
            "--since",
            since,
            "integration-service",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    wanted = set(correlation_ids)
    count = 0
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (
            record.get("message") == "request"
            and record.get("correlation_id") in wanted
            and str(record.get("path", "")).endswith("/disburse")
        ):
            count += 1
    return count


def events(op_key: str) -> str:
    return sql(
        INTEGRATION_DB,
        "SELECT count(*) FROM operation_events e JOIN operations o ON o.id = e.operation_id "
        "WHERE o.op_key = :'key'",
        key=op_key,
    )


def main() -> int:
    partner = Partner()
    uid = create_client(partner)
    loan_reference, _, approved = apply_and_approve(partner, uid, PRINCIPAL)
    if approved.status != 200:
        print(f"approval failed: {approved}")
        return 1
    ref = reference("I1-DSB")
    body = {"external_reference": ref}
    print(f"I-1 run {RUN}: client_uid {uid}, loan_reference {loan_reference}, reference {ref}")

    before = (
        f"{fineract_view(fineract_loan(loan_reference))}; Fineract disbursements "
        f"{fineract_disbursements(loan_reference)}; ERP disbursement entries "
        f"{erp_disbursements(loan_reference)}; DSB operations "
        f"{operations_count(op_type='DSB', loan_reference=loan_reference)}"
    )
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    replies = [
        partner.call("POST", f"/api/loans/{loan_reference}/disburse", body) for _ in range(TIMES)
    ]
    entry = erp_entry(f"DSB:{ref}")

    received = requests_logged(since, [r.correlation_id for r in replies])
    in_fineract = fineract_disbursements(loan_reference)
    in_erp = erp_disbursements(loan_reference)
    operations = operations_count(op_type="DSB", loan_reference=loan_reference)
    commands = fineract_commands(f"DSB:{ref}")
    first = replies[0]
    same = all(r.status == 200 and r.body == first.body for r in replies[1:])

    counts = (
        f"Requests received:        {received}\n"
        f"Fineract disbursements:   {in_fineract}\n"
        f"ERP journal entries:      {in_erp}\n"
        f"Integration transactions: {operations}"
    )
    passed = (
        first.status == 200
        and same
        and received == TIMES
        and in_fineract == "1"
        and in_erp == 1
        and operations == "1"
        and commands == "1"
    )
    answers = "; ".join(f"call {n}: HTTP {r.status}" for n, r in enumerate(replies, start=1))
    evidence(
        f"I-1 The same disbursement sent {TIMES} times, one after another",
        passed,
        before,
        f"POST /api/loans/{loan_reference}/disburse {short(body)}, {TIMES} times in a row",
        f"{answers}; first body {short(first.body)}; calls 2-{TIMES} have the identical "
        f"body: {same}",
        f"{fineract_view(fineract_loan(loan_reference))}; disbursement transactions: "
        f"{in_fineract}; commands run with Idempotency-Key DSB:{ref}:<attempt>: {commands}",
        f"{in_erp} DISBURSEMENT entry for {loan_reference}: {show_entry(entry)}",
        f"{history(partner, ref)}; operations rows: {operations}; operation_events rows: "
        f"{events(f'DSB:{ref}')}",
        "4 requests received; 1 Fineract disbursement; 1 ERP journal entry; 1 integration "
        "transaction; every answer 200 with the same body",
        "; ".join(counts.splitlines()) + f"; Fineract commands for the operation: {commands}",
        "the first call saves operation DSB:<reference> and pays out once; calls 2-4 find the "
        "finished operation with the same request hash and get its saved answer. Fineract is "
        "not called again, and the outbox has one row, so the ERP gets one entry.",
    )
    print(f"\n{counts}")
    partner.close()
    print(f"\nResult: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
