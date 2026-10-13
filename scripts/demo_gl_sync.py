"""Part A demo: GL sync behaviours 1-4 on the running stack, in the evidence template.

Run from the repo root, after setup (make up) has synced the ERP chart:

    make demo-gl-sync
    # or: uv run --project integration-service python scripts/demo_gl_sync.py

It uses only a real ERP account (GL-1200) and never creates a GL account. The one thing it
may create is a throwaway Fineract user "demo-no-sync" (role "Demo No Sync", only
READ_GLACCOUNT, in Head Office), made once and reused, to show the 403 in behaviour 4.
Every run gives the same results. Database counts are read with psql inside the db
container, in a read-only transaction.

Exit code 0 when every behaviour matched what should happen, 1 otherwise."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from integration_service.clients.erp import ErpAccount, ErpClient
from integration_service.clients.errors import FineractRejected
from integration_service.clients.fineract import FineractClient
from integration_service.mapping.account_types import build_fineract_gl_body
from integration_service.setup.config_files import ApiUserConfig, RoleConfig
from integration_service.setup.steps import ensure_api_user, ensure_role, find_user

REPO = Path(__file__).resolve().parents[1]
REFERENCE = "GL-1200"

DEMO_ROLE = RoleConfig(
    name="Demo No Sync",
    description="Throwaway demo role: can read ERP GL mappings, cannot sync them",
    permissions=["READ_GLACCOUNT"],
)
DEMO_USER = ApiUserConfig(
    username="demo-no-sync",
    firstname="Demo",
    lastname="NoSync",
    email="demo-no-sync@inkomoko.local",
    office="Head Office",
    role="Demo No Sync",
)
DEMO_PASSWORD = "Demo-No-Sync-2026!"  # noqa: S105 (throwaway local demo user)


# --- stack access (shared with simulate_gl_sync_4x.py) -------------------------------------


def dotenv() -> dict[str, str]:
    """The repo .env values, overridden by the real environment."""
    values: dict[str, str] = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values | dict(os.environ)


ENV = dotenv()
FINERACT_URL = ENV.get("DEMO_FINERACT_URL", "http://localhost:8080/fineract-provider/api/v1")
ERP_URL = ENV.get("DEMO_ERP_URL", "http://localhost:8090")
SERVICE_URL = ENV.get("DEMO_SERVICE_URL", "http://localhost:8091")


def fineract(username: str | None = None, password: str | None = None) -> FineractClient:
    return FineractClient(
        FINERACT_URL,
        "default",
        username or ENV.get("FINERACT_ADMIN_USER", "mifos"),
        password or ENV.get("FINERACT_ADMIN_PASSWORD", "password"),
        30.0,
    )


def erp() -> ErpClient:
    return ErpClient(ERP_URL, ENV.get("ERP_API_KEY", "change-me-erp-key"), 10.0)


def partner_key() -> str:
    first = ENV.get("PARTNER_API_KEYS", "demo-partner:change-me-partner-key").split(",")[0]
    return first.partition(":")[2].strip()


def sql(database: str, query: str, **params: str) -> str:
    """One value from psql in the db container, inside a read-only transaction.
    Values go in as psql variables (:'name' in the query), never pasted into the SQL."""
    variables = [arg for name, value in params.items() for arg in ("-v", f"{name}={value}")]
    result = subprocess.run(  # noqa: S603 (fixed argument list, our own SQL)
        [  # noqa: S607
            "docker",
            "compose",
            "-f",
            str(REPO / "docker-compose.yml"),
            "exec",
            "-T",
            "-e",
            "PGOPTIONS=-c default_transaction_read_only=on",
            "db",
            "psql",
            "-U",
            ENV.get("POSTGRES_USER", "postgres"),
            "-d",
            database,
            "-tAX",
            "-v",
            "ON_ERROR_STOP=1",
            *variables,
            "-f",
            "-",
        ],
        input=query,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return f"(psql failed: {result.stderr.strip()})"
    return result.stdout.strip()


def fineract_counts(reference: str, code: str) -> dict[str, str]:
    return {
        "glsync_external_ref rows": sql(
            "fineract_default",
            "SELECT count(*) FROM glsync_external_ref WHERE external_reference = :'ref'",
            ref=reference,
        ),
        f"acc_gl_account rows with gl_code {code}": sql(
            "fineract_default",
            "SELECT count(*) FROM acc_gl_account WHERE gl_code = :'code'",
            code=code,
        ),
    }


def service_row(reference: str) -> str:
    """What the Integration Service reports for this account (GET /api/accounts/sync)."""
    try:
        response = httpx.get(
            f"{SERVICE_URL}/api/accounts/sync",
            headers={"X-API-Key": partner_key()},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        return f"not reachable ({type(exc).__name__})"
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    for account in response.json()["accounts"]:
        if account["erp_reference"] == reference:
            return f"status {account['status']}, attempts {account['attempts']}"
    return "no gl_sync row for it yet"


def erp_account(client: ErpClient, reference: str) -> tuple[ErpAccount, list[ErpAccount]]:
    chart = client.list_accounts()
    matches = [a for a in chart if a.external_reference == reference]
    if len(matches) != 1:
        sys.exit(f"The ERP has {len(matches)} accounts with reference {reference}, expected 1")
    return matches[0], chart


def body_for(account: ErpAccount, chart: list[ErpAccount]) -> dict[str, Any]:
    """The exact body the Integration Service sends for this account."""
    parents = [a.external_reference for a in chart if a.code == account.parent_code]
    return build_fineract_gl_body(account, parents[0] if parents else None)


def correlation() -> str:
    return f"c-demo-{uuid.uuid4().hex[:12]}"


# --- evidence template --------------------------------------------------------------------

_LABELS = (
    "What things looked like before",
    "The request",
    "The response",
    "What Fineract shows",
    "What the ERP shows",
    "What the Integration Service shows",
    "What should happen",
    "What did happen",
    "Why",
)


def evidence(title: str, passed: bool, *points: str) -> bool:
    print(f"\n=== {title} [{'PASS' if passed else 'FAIL'}] ===")
    for n, (label, text) in enumerate(zip(_LABELS, points, strict=True), start=1):
        print(f"{n}. {label}: {text}")
    return passed


def attempt(call: Callable[[], Any]) -> tuple[Any, FineractRejected | None]:
    try:
        return call(), None
    except FineractRejected as exc:
        return None, exc


def describe(exc: FineractRejected) -> str:
    return f"HTTP {exc.status} {', '.join(exc.error_codes) or exc.message}"


# --- the four behaviours ------------------------------------------------------------------


def main() -> int:
    admin, chart_client = fineract(), erp()
    account, chart = erp_account(chart_client, REFERENCE)
    body = body_for(account, chart)
    erp_view = (
        f"{account.external_reference} code {account.code} '{account.name}' "
        f"{account.type} {account.usage}, parent {account.parent_code}"
    )
    mapping = admin.get_erp_account(REFERENCE)
    if mapping is None:
        print(f"{REFERENCE} is not synced to Fineract yet. Run setup first (make up).")
        return 1
    results = []

    # 1. The ERP reference is stored with the GL account, once.
    counts = fineract_counts(REFERENCE, account.code)
    unique = sql(
        "fineract_default",
        "SELECT string_agg(conname, ', ' ORDER BY conname) FROM pg_constraint "
        "WHERE conrelid = 'glsync_external_ref'::regclass AND contype = 'u'",
    )
    stored = all(v == "1" for v in counts.values())
    results.append(
        evidence(
            "1. The ERP reference is stored and unique",
            stored and "uq_glsync_external_ref_reference" in unique,
            f"{REFERENCE} was synced earlier by setup",
            f"GET /erp-integration/accounts/{REFERENCE} (lookup, admin)",
            f"200 glAccountId {mapping.gl_account_id}, glCode {mapping.gl_code}, "
            f"name '{mapping.name}', parent {mapping.parent_account}",
            f"{counts}; unique constraints on glsync_external_ref: {unique}",
            erp_view,
            service_row(REFERENCE),
            "exactly one mapping row and one GL account; the reference column is UNIQUE",
            f"counts {list(counts.values())}, constraints {unique}",
            "the plugin keeps the reference in its own table with a UNIQUE constraint, so a "
            "second GL account for the same reference cannot be saved",
        )
    )

    # 2. Same reference, identical payload: the same GL account comes back.
    before = fineract_counts(REFERENCE, account.code)
    result, rejected = attempt(
        lambda: admin.put_erp_account(REFERENCE, body, correlation_id=correlation())
    )
    after = fineract_counts(REFERENCE, account.code)
    same = (
        rejected is None
        and result.outcome == "UNCHANGED"
        and result.gl_account_id == mapping.gl_account_id
        and before == after
    )
    results.append(
        evidence(
            "2. Same reference + identical payload -> same GL account",
            same,
            f"{before}",
            f"PUT /erp-integration/accounts/{REFERENCE} with the body the service sends: {body}",
            describe(rejected)
            if rejected
            else f"200 outcome {result.outcome}, glAccountId {result.gl_account_id}",
            f"after: {after}",
            erp_view,
            service_row(REFERENCE),
            f"200 UNCHANGED with glAccountId {mapping.gl_account_id}; counts do not change",
            describe(rejected)
            if rejected
            else f"{result.outcome}, glAccountId {result.gl_account_id}, counts {after}",
            "the plugin compares the payload hash for this reference and finds it equal, so it "
            "returns the GL account it already has and writes nothing",
        )
    )

    # 3. Same reference, different payload: 409, nothing changes.
    changed = body | {"name": f"{body['name']} (changed)"}
    before = fineract_counts(REFERENCE, account.code)
    result, rejected = attempt(
        lambda: admin.put_erp_account(REFERENCE, changed, correlation_id=correlation())
    )
    after_mapping = admin.get_erp_account(REFERENCE)
    after = fineract_counts(REFERENCE, account.code)
    conflict = (
        rejected is not None
        and rejected.status == 409
        and "error.msg.glaccount.sync.payload.conflict" in rejected.error_codes
        and after_mapping is not None
        and after_mapping.name == mapping.name
        and before == after
    )
    results.append(
        evidence(
            "3. Same reference + different payload -> 409",
            conflict,
            f"name '{mapping.name}'; {before}",
            f"PUT /erp-integration/accounts/{REFERENCE} with name '{changed['name']}'",
            describe(rejected) if rejected else f"200 {result.outcome} (unexpected)",
            f"name still '{after_mapping.name if after_mapping else None}'; {after}",
            erp_view + " (the ERP never changed; only this request did)",
            service_row(REFERENCE),
            "409 error.msg.glaccount.sync.payload.conflict, nothing written",
            describe(rejected) if rejected else "no conflict",
            "a reference stands for one account; updates are out of scope, so different data "
            "for a known reference is refused instead of overwriting",
        )
    )

    # 4. A user without SYNC_GLACCOUNT gets 403.
    user_existed = find_user(admin, DEMO_USER.username) is not None
    ensure_role(admin, DEMO_ROLE)
    ensure_api_user(admin, DEMO_USER, DEMO_PASSWORD)
    demo = fineract(DEMO_USER.username, DEMO_PASSWORD)
    before = fineract_counts(REFERENCE, account.code)
    result, rejected = attempt(
        lambda: demo.put_erp_account(REFERENCE, body, correlation_id=correlation())
    )
    can_read = demo.get_erp_account(REFERENCE) is not None
    after = fineract_counts(REFERENCE, account.code)
    forbidden = rejected is not None and rejected.status == 403 and can_read and before == after
    results.append(
        evidence(
            "4. A user without SYNC_GLACCOUNT -> 403",
            forbidden,
            f"user {DEMO_USER.username} {'existed' if user_existed else 'was created now'} "
            f"with role '{DEMO_ROLE.name}' = {DEMO_ROLE.permissions}; {before}",
            f"PUT /erp-integration/accounts/{REFERENCE} as {DEMO_USER.username}, same body",
            describe(rejected) if rejected else f"200 {result.outcome} (unexpected)",
            f"{after}; the same user can still GET the mapping: {can_read}",
            erp_view,
            service_row(REFERENCE),
            "403 error.msg.not.authorized; reading with READ_GLACCOUNT still works",
            describe(rejected) if rejected else "not refused",
            "the plugin's PUT needs the SYNC_GLACCOUNT permission; this role only has "
            "READ_GLACCOUNT",
        )
    )

    for client in (admin, demo, chart_client):
        client.close()
    print(f"\n{sum(results)} of {len(results)} behaviours matched")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
