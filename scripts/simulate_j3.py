"""J-3: Fineract times out after the request was sent.

Run from the repo root on the running stack (outbox on):

    make simulate-j3

The fault proxy is set to `timeout_after_send` for this loan's disburse call: the write
reaches Fineract and Fineract pays out, but its answer is held past the service's Fineract
timeout (30 s) and then dropped, and the proxy goes down, so the lookup right after fails
too. Expected: 504 FINERACT_TIMEOUT, the operation stays FINERACT_PENDING, a repeat gets
202. Then Fineract comes back (fault off). Once the operation is stale (about 3 minutes),
recovery (the background loop, or POST /admin/recover) looks it up by externalId, finds
it and finishes it, with no second write: 1 Fineract disbursement and 1 Fineract command
for the operation, 1 ERP entry. Takes about 4 minutes, mostly idle waiting.
Every fault is off at the end. Exit code 0 when all of that holds, 1 otherwise."""

from __future__ import annotations

import time

from demo_gl_sync import ENV, evidence
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

# A little longer than the service's Fineract timeout, so its call really times out.
HOLD_SECONDS = float(ENV.get("INTEGRATION_FINERACT_TIMEOUT_SECONDS", "30")) + 2
STALE_WAIT = 300.0


def main() -> int:
    partner = Partner()
    try:
        faults_off()
        loan = approved_loan(partner)
        ref = reference("J3-DSB")
        key = f"DSB:{ref}"
        body = {"external_reference": ref}
        path = f"/api/loans/{loan}/disburse"
        print(f"J-3 run {RUN}: loan_reference {loan}, reference {ref}")
        before = f"Fineract: {fineract_state(loan, key)}; ERP: {erp_state(key)}; no operation"

        proxy_fault(
            "timeout_after_send",
            delay_seconds=HOLD_SECONDS,
            path_contains=f"{loan}?command=disburse",
        )
        started = time.monotonic()
        unclear = partner.call("POST", path, body)
        took = time.monotonic() - started
        switch = proxy_fault(None)  # Fineract is back
        repeat = partner.call("POST", path, body)
        during_f, during_e, during_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        during_ok = (
            unclear.status == 504
            and unclear.code() == "FINERACT_TIMEOUT"
            and repeat.status == 202
            and op_status(key) == "FINERACT_PENDING"
            and switch["dropped_answers"] >= 1
            and fineract_disbursements(loan) == "1"
            and erp_count(key) == "0"
        )

        print(f"waiting up to {STALE_WAIT:.0f} s for recovery (stale after about 180 s) ...")
        recover = None
        deadline = time.monotonic() + STALE_WAIT
        while time.monotonic() < deadline and op_status(key) == "FINERACT_PENDING":
            if time.monotonic() - started > 185 and recover is None:
                recover = partner.call("POST", "/admin/recover")
            time.sleep(5)
        done = wait_for(lambda: op_status(key) == "COMPLETED", 60)
        replay = partner.call("POST", path, body)
        after_f, after_e, after_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        passed = (
            during_ok
            and done
            and replay.status == 200
            and fineract_disbursements(loan) == "1"
            and fineract_commands(key) == "1"
            and erp_count(key) == "1"
        )
        if recover is None:
            recovered_by = "the background loop, before the script called POST /admin/recover"
        elif recover.status == 200 and recover.body["operations"]["completed"] == 1:
            recovered_by = f"POST /admin/recover HTTP 200 {short(recover.body)}"
        else:
            recovered_by = (
                "the background loop's pass (it held the lease; POST /admin/recover at the "
                f"same moment answered HTTP {recover.status} {short(recover.body)})"
            )
        evidence(
            "J-3 Fineract times out after the request was sent",
            passed,
            before,
            f"proxy timeout_after_send (hold {HOLD_SECONDS:.0f} s, then drop; then down) for "
            f"{loan}?command=disburse; POST {path} {short(body)}; fault off; the same request "
            "again; wait for recovery; the same request again",
            f"first: HTTP {unclear.status} after {took:.1f} s {short(unclear.body)} || repeat "
            f"while pending: HTTP {repeat.status} {short(repeat.body)} || after recovery: HTTP "
            f"{replay.status} {short(replay.body)}",
            f"during: {during_f} || after: {after_f}",
            f"during: {during_e} || after: {after_e}",
            f"during: {during_i} || proxy: {switch['last_dropped']} || recovered by: "
            f"{recovered_by} || after: {after_i}",
            "504 FINERACT_TIMEOUT and FINERACT_PENDING (Fineract already paid out); 202 for a "
            "repeat; recovery finds it by externalId and finishes it; still 1 disbursement and "
            "1 Fineract command (no second write); 1 ERP entry; replay 200",
            f"first {unclear.status} {unclear.code()}, repeat {repeat.status}, checks held: "
            f"{during_ok}; completed {done}; Fineract disbursements "
            f"{fineract_disbursements(loan)}, commands {fineract_commands(key)}; ERP entries "
            f"{erp_count(key)}; replay {replay.status}",
            "a timeout means the outcome is unknown, so nothing is sent again blindly. The "
            "lookup right after failed (Fineract unreachable), so the operation stays "
            "FINERACT_PENDING and a repeat gets 202. Recovery looks the transaction up by our "
            "externalId (DSB:<reference>), finds Fineract's own transaction, and finishes it "
            "the way the normal path would (FINERACT_DONE, saved answer and outbox row in one "
            "transaction). It never posts to Fineract.",
        )
        print(f"\nResult: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        print(f"\nFaults reset: {faults_off()}")
        partner.close()


if __name__ == "__main__":
    raise SystemExit(main())
