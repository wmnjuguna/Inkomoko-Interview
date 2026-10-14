"""J-4: the ERP times out after it saved the entry.

Run from the repo root on the running stack (outbox on):

    make simulate-j4

The ERP fault `timeout_after_write` (for the next journal call only) saves the entry and
then holds the answer past the service's ERP timeout (10 s). The outbox sees a timeout and
sends the same entry again; the ERP answers 200 (already there). Expected: 1 Fineract
disbursement, exactly 1 ERP entry, outbox attempts 2, operation COMPLETED.
Every fault is off at the end. Exit code 0 when all of that holds, 1 otherwise."""

from __future__ import annotations

from demo_gl_sync import evidence, sql
from demo_identity import INTEGRATION_DB, RUN, Partner, reference, short
from simulate_j_common import (
    approved_loan,
    erp_count,
    erp_fault,
    erp_state,
    faults_off,
    fineract_disbursements,
    fineract_state,
    integration_state,
    op_status,
    outbox_attempts,
    wait_for,
)

HOLD_SECONDS = 12  # longer than the service's ERP timeout (10 s)


def timeout_events(op_key: str) -> str:
    """Status moves recorded for a send that got no answer from the ERP."""
    return sql(
        INTEGRATION_DB,
        "SELECT count(*) FROM operation_events e JOIN operations o ON o.id = e.operation_id "
        "WHERE o.op_key = :'k' AND e.detail LIKE 'no answer from the ERP%'",
        k=op_key,
    )


def main() -> int:
    partner = Partner()
    try:
        faults_off()
        loan = approved_loan(partner)
        ref = reference("J4-DSB")
        key = f"DSB:{ref}"
        body = {"external_reference": ref}
        print(f"J-4 run {RUN}: loan_reference {loan}, reference {ref}")
        before = f"Fineract: {fineract_state(loan, key)}; ERP: {erp_state(key)}; no operation"

        erp_fault("timeout_after_write", delay_seconds=HOLD_SECONDS, remaining=1)
        reply = partner.call("POST", f"/api/loans/{loan}/disburse", body)
        # The first send: saved by the ERP, but no answer within 10 s.
        first_failed = wait_for(lambda: outbox_attempts(key) >= 1 and erp_count(key) == "1", 20)
        timed_out = wait_for(lambda: timeout_events(key) == "1", 20, 0.5)
        during_f, during_e, during_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        done = wait_for(lambda: op_status(key) == "COMPLETED", 90)
        after_f, after_e, after_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        attempts = outbox_attempts(key)
        passed = (
            reply.status == 200
            and first_failed
            and timed_out
            and done
            and attempts == 2
            and fineract_disbursements(loan) == "1"
            and erp_count(key) == "1"
        )
        evidence(
            "J-4 The ERP times out after it saved the entry",
            passed,
            before,
            f"ERP fault timeout_after_write (hold {HOLD_SECONDS} s, next journal call only); "
            f"POST /api/loans/{loan}/disburse {short(body)}",
            f"HTTP {reply.status} {short(reply.body)}",
            f"during: {during_f} || after: {after_f}",
            f"during (after the first send timed out): {during_e} || after: {after_e}",
            f"during: {during_i} || after: {after_i}; events for a send with no answer from "
            f"the ERP: {timeout_events(key)}",
            "the first send times out although the ERP saved the entry; the outbox sends it "
            "again, the ERP answers 200 (already there); exactly 1 ERP entry, outbox attempts "
            "2, COMPLETED",
            f"first send timed out: {timed_out}; ERP held the entry during: {first_failed}; "
            f"completed {done}; outbox attempts {attempts}; ERP entries {erp_count(key)}; "
            f"Fineract disbursements {fineract_disbursements(loan)}",
            "a timeout from the ERP leaves the outcome unknown, so the outbox row stays "
            "PENDING and is sent again after a short backoff. The ERP is idempotent by "
            "external_reference: the same entry again gets 200 with the first entry, never a "
            "second entry. The outbox then marks the row SENT.",
        )
        print(f"\nResult: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        print(f"\nFaults reset: {faults_off()}")
        partner.close()


if __name__ == "__main__":
    raise SystemExit(main())
