"""J-1: the ERP is down after Fineract succeeds.

Run from the repo root on the running stack (outbox on):

    make simulate-j1

The ERP fault `down` is switched on for /api/journal-entries only. A disbursement goes
through: Fineract pays out, the partner gets 200 (status ERP_PENDING), the outbox tries the
ERP and backs off. Then the fault is switched off and the outbox posts the entry. Fineract,
integration_db and erp_db are read before, during and after. Expected: 1 Fineract
disbursement, 1 ERP entry, operation COMPLETED, outbox attempts >= 2.
Every fault is off at the end. Exit code 0 when all of that holds, 1 otherwise."""

from __future__ import annotations

from demo_gl_sync import evidence
from demo_identity import RUN, Partner, reference, short
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


def main() -> int:
    partner = Partner()
    try:
        faults_off()
        loan = approved_loan(partner)
        ref = reference("J1-DSB")
        key = f"DSB:{ref}"
        print(f"J-1 run {RUN}: loan_reference {loan}, reference {ref}")
        before = f"Fineract: {fineract_state(loan, key)}; ERP: {erp_state(key)}; no operation"

        erp_fault("down")
        reply = partner.call("POST", f"/api/loans/{loan}/disburse", {"external_reference": ref})
        retried = wait_for(lambda: outbox_attempts(key) >= 2, 20)
        during_f, during_e, during_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        during_ok = op_status(key) == "ERP_PENDING" and erp_count(key) == "0" and retried

        erp_fault(None)
        done = wait_for(lambda: op_status(key) == "COMPLETED", 90)
        repeat = partner.call("POST", f"/api/loans/{loan}/disburse", {"external_reference": ref})
        after_f, after_e, after_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        passed = (
            reply.status == 200
            and reply.body.get("status") == "ERP_PENDING"
            and during_ok
            and done
            and fineract_disbursements(loan) == "1"
            and erp_count(key) == "1"
            and repeat.status == 200
        )
        evidence(
            "J-1 The ERP is down after Fineract succeeds",
            passed,
            before,
            f"ERP fault down on /api/journal-entries; POST /api/loans/{loan}/disburse "
            f"{short({'external_reference': ref})}; after the outbox retried, fault off; "
            "then the same request again",
            f"HTTP {reply.status} {short(reply.body)}; repeat HTTP {repeat.status}",
            f"during: {during_f} || after: {after_f}",
            f"during: {during_e} || after: {after_e}",
            f"during: {during_i} || after: {after_i}",
            "partner gets 200 ERP_PENDING at once; the outbox retries with backoff while the "
            "ERP is down; after the fault is off: 1 Fineract disbursement, 1 ERP entry, "
            "COMPLETED",
            f"answer {reply.status}; outbox retried while down: {retried}; completed: {done}; "
            f"Fineract disbursements {fineract_disbursements(loan)}; ERP entries "
            f"{erp_count(key)}",
            "the outbox row was written in the same transaction as FINERACT_DONE, so the ERP "
            "outage cannot lose it. Each failed send backs off (1, 2, 4 ... s). The ERP is "
            "idempotent by external_reference and a row is SENT once, so there is no double "
            "posting.",
        )
        print(f"\nResult: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        print(f"\nFaults reset: {faults_off()}")
        partner.close()


if __name__ == "__main__":
    raise SystemExit(main())
