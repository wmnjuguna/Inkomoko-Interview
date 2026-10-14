"""Shared parts of the Part J simulations (simulate_j1.py ... simulate_j5.py).

Fault switches: the Fineract fault proxy (admin on localhost:8092) and the Mock ERP's
/admin/faults. State is read from each system's own store, read only: Fineract's tables,
integration_db (operation, its status moves, its outbox row) and erp_db (the ERP's journal
table, so a fault on the ERP's API cannot hide what it holds)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from demo_gl_sync import ENV, sql
from demo_identity import INTEGRATION_DB, Partner
from demo_lifecycle import apply_and_approve, create_client, fineract_loan, fineract_view

PROXY_ADMIN = ENV.get("FAULT_PROXY_ADMIN_URL", "http://localhost:8092")
PROXY_KEY = ENV.get("FAULT_PROXY_API_KEY", "change-me-fault-proxy-key")
ERP_URL = ENV.get("DEMO_ERP_URL", "http://localhost:8090")
ERP_KEY = ENV.get("ERP_API_KEY", "change-me-erp-key")
JOURNAL = "/api/journal-entries"
PRINCIPAL = 300_000


def proxy_fault(mode: str | None, **extra: Any) -> dict[str, Any]:
    """Turn the Fineract proxy fault on (mode) or off (None). Returns the switch."""
    url, headers = f"{PROXY_ADMIN}/admin/faults", {"X-API-Key": PROXY_KEY}
    if mode is None:
        response = httpx.delete(url, headers=headers, timeout=5)
    else:
        response = httpx.put(url, json={"mode": mode, **extra}, headers=headers, timeout=5)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


def erp_fault(mode: str | None, **extra: Any) -> dict[str, Any]:
    """Turn the ERP fault on for the journal path only (mode), or off (None)."""
    url, headers = f"{ERP_URL}/admin/faults", {"X-API-Key": ERP_KEY}
    if mode is None:
        response = httpx.delete(url, headers=headers, timeout=5)
    else:
        body = {"mode": mode, "path_prefix": JOURNAL, **extra}
        response = httpx.put(url, json=body, headers=headers, timeout=5)
    response.raise_for_status()
    shown: dict[str, Any] = response.json()
    return shown


def faults_off() -> str:
    proxy = proxy_fault(None)
    erp = erp_fault(None)
    return f"proxy fault {proxy['mode']}, ERP fault active {erp['active']}"


def approved_loan(partner: Partner) -> str:
    uid = create_client(partner)
    loan_reference, _, approved = apply_and_approve(partner, uid, PRINCIPAL)
    if approved.status != 200:
        raise SystemExit(f"approval failed: {approved}")
    return loan_reference


# --- state, each from its own store ----------------------------------------------------------


def fineract_disbursements(loan_reference: str) -> str:
    return sql(
        "fineract_default",
        "SELECT count(*) FROM m_loan_transaction t JOIN m_loan l ON l.id = t.loan_id "
        "WHERE l.external_id = :'loan' AND t.transaction_type_enum = 1 AND NOT t.is_reversed",
        loan=loan_reference,
    )


def fineract_commands(op_key: str) -> str:
    """Writes Fineract ran with an Idempotency-Key of this operation (<op key>:<attempt>)."""
    return sql(
        "fineract_default",
        "SELECT count(*) FROM m_portfolio_command_source WHERE idempotency_key LIKE :'keys'",
        keys=f"{op_key}:%",
    )


def fineract_state(loan_reference: str, op_key: str) -> str:
    return (
        f"{fineract_view(fineract_loan(loan_reference))}; disbursement transactions "
        f"{fineract_disbursements(loan_reference)}; commands with Idempotency-Key "
        f"{op_key}:<attempt>: {fineract_commands(op_key)}"
    )


def erp_count(op_key: str) -> str:
    return sql(
        "erp_db", "SELECT count(*) FROM journal_entries WHERE external_reference = :'r'", r=op_key
    )


def erp_state(op_key: str) -> str:
    return f"journal entries with reference {op_key}: {erp_count(op_key)}"


def op_status(op_key: str) -> str:
    return sql(INTEGRATION_DB, "SELECT status FROM operations WHERE op_key = :'k'", k=op_key)


def outbox_attempts(op_key: str) -> int:
    found = sql(
        INTEGRATION_DB,
        "SELECT x.attempts FROM outbox x JOIN operations o ON o.id = x.operation_id "
        "WHERE o.op_key = :'k'",
        k=op_key,
    )
    return int(found) if found.isdigit() else 0


def integration_state(op_key: str) -> str:
    """The operation, its status moves (operation_events) and its outbox row."""
    found = sql(
        INTEGRATION_DB,
        "SELECT 'operation ' || o.status || ', attempts ' || o.attempts || ', erp_status ' "
        "|| coalesce(o.erp_status, '-') || '; events ' || coalesce((SELECT string_agg("
        "coalesce(e.from_status, '') || '->' || e.to_status, ', ' ORDER BY e.id) FROM "
        "operation_events e WHERE e.operation_id = o.id), 'none') || '; outbox ' || "
        "coalesce(x.status || ', attempts ' || x.attempts || ', last_error ' || "
        "coalesce(x.last_error, '-'), 'no row') FROM operations o LEFT JOIN outbox x ON "
        "x.operation_id = o.id WHERE o.op_key = :'k'",
        k=op_key,
    )
    return found or f"no operation {op_key}"


def wait_for(check: Callable[[], bool], seconds: float, every: float = 1.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(every)


def snapshot(loan_reference: str, op_key: str) -> str:
    return (
        f"Fineract: {fineract_state(loan_reference, op_key)} | Integration: "
        f"{integration_state(op_key)} | ERP: {erp_state(op_key)}"
    )
