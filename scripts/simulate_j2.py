"""J-2: Fineract is down.

Run from the repo root on the running stack:

    make simulate-j2

The fault proxy between the Integration Service and Fineract stops listening (`down`), so
the service's connection is refused. A disbursement must answer 503 FINERACT_UNAVAILABLE
with the operation FAILED, no Fineract transaction, no outbox row and no ERP entry. Then
Fineract comes back (fault off) and the same request with the same reference is paid out
once. Every fault is off at the end. Exit code 0 when all of that holds, 1 otherwise."""

from __future__ import annotations

from demo_gl_sync import evidence
from demo_identity import RUN, Partner, reference, short
from simulate_j_common import (
    approved_loan,
    erp_count,
    erp_state,
    faults_off,
    fineract_commands,
    fineract_disbursements,
    fineract_state,
    integration_state,
    op_status,
    proxy_fault,
    wait_for,
)


def main() -> int:
    partner = Partner()
    try:
        faults_off()
        loan = approved_loan(partner)
        ref = reference("J2-DSB")
        key = f"DSB:{ref}"
        body = {"external_reference": ref}
        print(f"J-2 run {RUN}: loan_reference {loan}, reference {ref}")
        before = f"Fineract: {fineract_state(loan, key)}; ERP: {erp_state(key)}; no operation"

        proxy_fault("down")
        refused = partner.call("POST", f"/api/loans/{loan}/disburse", body)
        during_f, during_e, during_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        during_ok = (
            refused.status == 503
            and refused.code() == "FINERACT_UNAVAILABLE"
            and op_status(key) == "FAILED"
            and fineract_disbursements(loan) == "0"
            and fineract_commands(key) == "0"
            and "no row" in during_i
            and erp_count(key) == "0"
        )

        proxy_fault(None)
        retried = partner.call("POST", f"/api/loans/{loan}/disburse", body)
        done = wait_for(lambda: op_status(key) == "COMPLETED", 60)
        after_f, after_e, after_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        passed = (
            during_ok
            and retried.status == 200
            and done
            and fineract_disbursements(loan) == "1"
            and fineract_commands(key) == "1"
            and erp_count(key) == "1"
        )
        evidence(
            "J-2 Fineract is down",
            passed,
            before,
            f"fault proxy down; POST /api/loans/{loan}/disburse {short(body)}; then fault off "
            "and the same request again",
            f"while down: HTTP {refused.status} {short(refused.body)} || after: HTTP "
            f"{retried.status} {short(retried.body)}",
            f"during: {during_f} || after: {after_f}",
            f"during: {during_e} || after: {after_e}",
            f"during: {during_i} || after: {after_i}",
            "while down: 503 FINERACT_UNAVAILABLE, operation FAILED, nothing in Fineract, no "
            "outbox row, no ERP entry. Retry with the same reference: 200, 1 disbursement, 1 "
            "ERP entry, attempts 2",
            f"while down: {refused.status} {refused.code()}, checks held: {during_ok}; retry: "
            f"{retried.status}, completed {done}; Fineract disbursements "
            f"{fineract_disbursements(loan)}; ERP entries {erp_count(key)}",
            "the first Fineract call (the disbursement template read, before the write) got a "
            "refused connection, which proves nothing reached Fineract, so the operation is FAILED "
            "(the only thing saved is the operation and its events, for the record). FAILED "
            "may be retried on the same row (attempts + 1): the retry first looks up the "
            "earlier attempt by externalId, finds nothing, and sends once.",
        )
        print(f"\nResult: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        print(f"\nFaults reset: {faults_off()}")
        partner.close()


if __name__ == "__main__":
    raise SystemExit(main())
