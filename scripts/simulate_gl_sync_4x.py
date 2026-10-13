"""Simulation: a new ERP account sent 4 times at once ends up once everywhere.

Run from the repo root, after setup (make up):

    make simulate-gl-sync-4x
    # or: uv run --project integration-service python scripts/simulate_gl_sync_4x.py

An account that is already synced would only show a replay, so each run first creates two
new, real ERP accounts in the Mock ERP: EXPENSE DETAIL accounts under header 5000, with an
8-digit code starting with 7 and a run nonce. Nothing is deleted.

Round 1, Integration Service: POST /api/accounts/<A>/sync four times at once. Only one call
may run the sync; the others answer 202 (a sync is running) or, if they come after it, 200
with nothing left to do. Fineract must have run exactly one command for A from the service
(counted by its Idempotency-Key GLS:<A>:... in m_portfolio_command_source).

Round 2, Fineract plugin: PUT /erp-integration/accounts/<B> four times at once with the
same body, for an account Fineract has never seen. Exactly one answer may be CREATED, the
others UNCHANGED (or a 409 refusal), and all successful answers give the same GL account.
Then one service sync for B records it.

For A and B it counts: ERP accounts with the reference, Fineract GL accounts with the code,
glsync_external_ref rows and gl_sync rows. Each must be 1, and both gl_sync rows SYNCED.
DEMO_INTEGRATION_DB names the database that holds gl_sync (default integration_db).
Exit code 0 when all of that holds."""

from __future__ import annotations

import secrets
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from demo_gl_sync import (
    ENV,
    ERP_URL,
    SERVICE_URL,
    body_for,
    correlation,
    erp,
    erp_account,
    fineract,
    partner_key,
    sql,
)
from integration_service.clients.errors import FineractError, FineractRejected

TIMES = 4
PARENT_CODE = "5000"
INTEGRATION_DB = ENV.get("DEMO_INTEGRATION_DB", "integration_db")


def new_erp_account(label: str) -> str:
    """Create a new EXPENSE DETAIL account in the Mock ERP. Returns its reference."""
    code = f"7{secrets.randbelow(10**7):07d}"
    reference = f"GL-{code}"
    response = httpx.post(
        f"{ERP_URL}/api/accounts",
        headers={"X-API-Key": ENV.get("ERP_API_KEY", "change-me-erp-key")},
        json={
            "code": code,
            "external_reference": reference,
            "name": f"Simulation {label} {code}",
            "type": "EXPENSE",
            "usage": "DETAIL",
            "parent_code": PARENT_CODE,
        },
        timeout=10,
    )
    if response.status_code != 201:
        raise SystemExit(f"Mock ERP did not create {reference}: {response.status_code}")
    return reference


def service_sync(n: int, reference: str) -> tuple[str, int]:
    try:
        response = httpx.post(
            f"{SERVICE_URL}/api/accounts/{reference}/sync",
            headers={"X-API-Key": partner_key(), "X-Correlation-ID": correlation()},
            timeout=120,
        )
    except httpx.HTTPError as exc:
        return f"call {n}: not reachable ({type(exc).__name__})", 0
    if response.status_code not in (200, 202):
        return f"call {n}: HTTP {response.status_code} {response.text[:200]}", response.status_code
    body = response.json()
    account = body["account"]
    state = f"{account['status']} attempts {account['attempts']}" if account else "no row yet"
    line = f"call {n}: {response.status_code} in_progress {body['in_progress']}, {state}"
    return line, response.status_code


def plugin_put(n: int, reference: str, body: dict[str, Any]) -> tuple[str, str, int | None]:
    client = fineract()
    try:
        result = client.put_erp_account(reference, body, correlation_id=correlation())
    except FineractRejected as exc:
        return f"call {n}: HTTP {exc.status} {', '.join(exc.error_codes)}", str(exc.status), None
    except FineractError as exc:
        return f"call {n}: no clear answer ({type(exc).__name__})", "error", None
    finally:
        client.close()
    line = f"call {n}: 200 {result.outcome} glAccountId {result.gl_account_id}"
    return line, result.outcome, result.gl_account_id


def counts_for(reference: str) -> tuple[dict[str, str], str]:
    chart_client = erp()
    account, _ = erp_account(chart_client, reference)
    erp_count = sum(a.external_reference == reference for a in chart_client.list_accounts())
    chart_client.close()
    admin = fineract()
    gl_accounts = [g for g in admin.get("/glaccounts") or [] if g.get("glCode") == account.code]
    admin.close()
    counts = {
        "ERP accounts with this reference": str(erp_count),
        f"Fineract GL accounts with code {account.code}": str(len(gl_accounts)),
        "glsync_external_ref rows (fineract_default)": sql(
            "fineract_default",
            "SELECT count(*) FROM glsync_external_ref WHERE external_reference = :'ref'",
            ref=reference,
        ),
        f"gl_sync rows ({INTEGRATION_DB})": sql(
            INTEGRATION_DB,
            "SELECT count(*) FROM gl_sync WHERE erp_reference = :'ref'",
            ref=reference,
        ),
    }
    status = sql(
        INTEGRATION_DB,
        "SELECT status || ' attempts ' || attempts FROM gl_sync WHERE erp_reference = :'ref'",
        ref=reference,
    )
    return counts, status


def show_counts(reference: str) -> bool:
    counts, status = counts_for(reference)
    print(f"\nCounts for {reference} (each should be 1):")
    for label, value in counts.items():
        print(f"  {label}: {value}")
    print(f"  gl_sync row: {status}")
    return all(v == "1" for v in counts.values()) and status.startswith("SYNCED")


def main() -> int:
    ref_a = new_erp_account("service race")
    ref_b = new_erp_account("plugin race")
    print(f"New ERP accounts: {ref_a} (round 1), {ref_b} (round 2), parent code {PARENT_CODE}")

    print(f"\nRound 1: POST {SERVICE_URL}/api/accounts/{ref_a}/sync x{TIMES} at once")
    with ThreadPoolExecutor(TIMES) as pool:
        calls = list(pool.map(lambda n: service_sync(n, ref_a), range(1, TIMES + 1)))
    for line, _ in calls:
        print(f"  {line}")
    round_1_ok = all(status in (200, 202) for _, status in calls)
    # Every send from the service carries Idempotency-Key GLS:<reference>:..., and Fineract
    # records each command it runs with its key.
    sends = sql(
        "fineract_default",
        "SELECT count(*) FROM m_portfolio_command_source WHERE idempotency_key LIKE :'keys'",
        keys=f"GLS:{ref_a}:%",
    )
    print(f"  commands Fineract ran for {ref_a} from the service (should be 1): {sends}")
    round_1_ok = round_1_ok and sends == "1"

    chart_client = erp()
    account_b, chart = erp_account(chart_client, ref_b)
    chart_client.close()
    body = body_for(account_b, chart)
    print(f"\nRound 2: PUT /erp-integration/accounts/{ref_b} x{TIMES} at once (same body)")
    with ThreadPoolExecutor(TIMES) as pool:
        puts = list(pool.map(lambda n: plugin_put(n, ref_b, body), range(1, TIMES + 1)))
    for line, _, _ in puts:
        print(f"  {line}")
    outcomes = Counter(outcome for _, outcome, _ in puts)
    gl_ids = {gl_id for _, _, gl_id in puts if gl_id is not None}
    print(f"  outcomes: {dict(outcomes)}; distinct glAccountIds: {len(gl_ids)}")
    round_2_ok = (
        outcomes["CREATED"] == 1
        and set(outcomes) <= {"CREATED", "UNCHANGED", "409"}
        and len(gl_ids) == 1
    )
    line, status = service_sync(1, ref_b)
    print(f"  then one service sync for {ref_b}: {line}")
    round_2_ok = round_2_ok and status == 200

    counts_ok = show_counts(ref_a) & show_counts(ref_b)
    ok = round_1_ok and round_2_ok and counts_ok
    verdict = {True: "PASS", False: "FAIL"}
    print(f"\nround 1: {verdict[round_1_ok]}, round 2: {verdict[round_2_ok]}")
    print("result:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
