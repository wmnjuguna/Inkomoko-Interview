"""J-5: a crash between Fineract succeeding and the ERP call.

Run from the repo root on the running stack:

    make simulate-j5

The Integration Service is recreated with FAULT_CRASH_AFTER_FINERACT_DONE set to this run's
reference prefix. The disbursement commits FINERACT_DONE (saved answer + outbox row,
ERP_PENDING), and then the process is killed (gunicorn master too) before any ERP call: the
partner gets no answer. Compose restarts the container (restart: unless-stopped); its
start-up recovery sends the outbox row. The partner's retry gets the saved answer.
Expected: 1 Fineract disbursement, exactly 1 ERP entry, operation COMPLETED, the container
restarted once. The service is always recreated without the flag at the end, and every
fault is off. Exit code 0 when all of that holds, 1 otherwise."""

from __future__ import annotations

import json
import os
import subprocess

import httpx
from demo_gl_sync import REPO, SERVICE_URL, evidence, sql
from demo_identity import INTEGRATION_DB, RUN, Partner, short
from simulate_j_common import (
    approved_loan,
    erp_count,
    erp_state,
    faults_off,
    fineract_disbursements,
    fineract_state,
    integration_state,
    op_status,
    wait_for,
)

CRASH_ENV = "FAULT_CRASH_AFTER_FINERACT_DONE"
SERVICE = "integration-service"


def compose(*args: str, crash_prefix: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != CRASH_ENV}
    if crash_prefix is not None:
        env[CRASH_ENV] = crash_prefix
    return subprocess.run(  # noqa: S603 (fixed argument list)
        ["docker", "compose", "-f", str(REPO / "docker-compose.yml"), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def recreate(crash_prefix: str | None) -> bool:
    """Recreate only the service (no dependencies), with or without the crash flag."""
    compose("up", "-d", "--no-build", "--no-deps", SERVICE, crash_prefix=crash_prefix)
    return wait_for(healthy, 90)


def healthy() -> bool:
    try:
        return httpx.get(f"{SERVICE_URL}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def container() -> str:
    """The container's state and restart count (docker inspect)."""
    found = compose("ps", "-a", "-q", SERVICE).stdout.strip()
    if not found:
        return "no container"
    result = subprocess.run(  # noqa: S603
        ["docker", "inspect", found.splitlines()[0]],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    data = json.loads(result.stdout)[0]
    state = data["State"]
    return (
        f"container {state['Status']}, exit code {state['ExitCode']}, restarts "
        f"{data['RestartCount']}"
    )


def crash_flag() -> str:
    result = compose("exec", "-T", SERVICE, "printenv", CRASH_ENV)
    return result.stdout.strip() or "(empty: off)"


def saved_body(op_key: str) -> object:
    raw = sql(INTEGRATION_DB, "SELECT response_body FROM operations WHERE op_key = :'k'", k=op_key)
    return json.loads(raw) if raw.startswith("{") else raw


def main() -> int:
    prefix = f"J5-{RUN}-"
    ref = f"{prefix}DSB"
    key = f"DSB:{ref}"
    body = {"external_reference": ref}
    partner = Partner()
    try:
        faults_off()
        loan = approved_loan(partner)
        path = f"/api/loans/{loan}/disburse"
        print(f"J-5 run {RUN}: loan_reference {loan}, reference {ref}")
        if not recreate(prefix):
            print("the service did not come up with the crash flag")
            return 1
        before = (
            f"Fineract: {fineract_state(loan, key)}; ERP: {erp_state(key)}; no operation; "
            f"{container()}; {CRASH_ENV}={crash_flag()}"
        )

        try:
            first = f"HTTP {partner.call('POST', path, body).status} (no crash?)"
        except httpx.TransportError as exc:
            first = f"no answer: {type(exc).__name__} (the service died mid-request)"
        during_f, during_e, during_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        during_status, during_erp, during_box = (
            op_status(key),
            erp_count(key),
            container(),
        )

        back = wait_for(healthy, 120)
        done = wait_for(lambda: op_status(key) == "COMPLETED", 60)
        restarted = container()
        retry = partner.call("POST", path, body)
        after_f, after_e, after_i = (
            fineract_state(loan, key),
            erp_state(key),
            integration_state(key),
        )
        passed = (
            first.startswith("no answer")
            and during_status == "ERP_PENDING"
            and during_erp == "0"
            and back
            and done
            and "restarts 1" in restarted
            and retry.status == 200
            and retry.body == saved_body(key)
            and fineract_disbursements(loan) == "1"
            and erp_count(key) == "1"
        )
        evidence(
            "J-5 A crash between Fineract succeeding and the ERP call",
            passed,
            before,
            f"POST {path} {short(body)} with the crash hook on for {prefix}*; after the "
            "restart, the same request again",
            f"first: {first} || retry after restart: HTTP {retry.status} {short(retry.body)} "
            f"(equals the saved answer: {retry.body == saved_body(key)})",
            f"during: {during_f} || after: {after_f}",
            f"during: {during_e} || after: {after_e}",
            f"during: {during_i}; {during_box} || after restart: {restarted} || after: {after_i}",
            "no answer to the first call; at the crash: ERP_PENDING, outbox PENDING, 0 ERP "
            "entries, 1 Fineract disbursement; compose restarts the service, start-up recovery "
            "sends the row once: COMPLETED, 1 ERP entry; the retry gets the saved answer",
            f"first: {first}; at the crash: {during_status}, ERP entries {during_erp}; back up "
            f"{back}, completed {done}; {restarted}; retry {retry.status}; Fineract "
            f"disbursements {fineract_disbursements(loan)}; ERP entries {erp_count(key)}",
            "FINERACT_DONE, the saved answer and the outbox row are one database transaction, "
            "so the crash cannot lose the journal entry or leave half a record. The outbox "
            "row is PENDING and due, so the first recovery tick after the restart sends it. "
            "The ERP is idempotent by external_reference, so even a send that raced the crash "
            "could not post twice. The retry finds the finished operation and replays its "
            "saved answer without calling Fineract.",
        )
        print(f"\nResult: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        up = recreate(None)
        print(
            f"\nService recreated without the crash flag: healthy {up}, {CRASH_ENV}="
            f"{crash_flag()}; faults reset: {faults_off()}"
        )
        partner.close()


if __name__ == "__main__":
    raise SystemExit(main())
