"""The clients against the running Fineract and Mock ERP from docker compose.

Fineract is shared and already holds data, so these tests never assume it is empty and
never delete anything. Each run creates its GL account under a fresh reference and code."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from typing import Any

import pytest

from integration_service.clients.erp import ErpClient
from integration_service.clients.errors import FineractConflict, FineractRejected
from integration_service.clients.fineract import FineractClient


@pytest.fixture(scope="module")
def fineract() -> Iterator[FineractClient]:
    client = FineractClient(
        base_url=os.environ.get(
            "FINERACT_BASE_URL", "http://localhost:8080/fineract-provider/api/v1"
        ),
        tenant=os.environ.get("FINERACT_TENANT", "default"),
        username=os.environ.get("FINERACT_USERNAME", "mifos"),
        password=os.environ.get("FINERACT_PASSWORD", "password"),
        timeout_seconds=30,
    )
    yield client
    client.close()


@pytest.fixture(scope="module")
def erp() -> Iterator[ErpClient]:
    client = ErpClient(
        base_url=os.environ.get("ERP_BASE_URL", "http://localhost:8090"),
        api_key=os.environ.get("ERP_API_KEY", "change-me-erp-key"),
        timeout_seconds=10,
    )
    yield client
    client.close()


def unique_account() -> tuple[str, dict[str, Any]]:
    """A fresh ERP reference and an 8-digit GL code starting with 7."""
    nonce = f"{secrets.randbelow(10**7):07d}"
    reference = f"IT-CLIENTS-{nonce}"
    body = {
        "name": f"Client test {nonce}",
        "glCode": f"7{nonce}",
        "type": 1,
        "usage": 2,
        "manualEntriesAllowed": False,
        "description": "Created by test_clients_live.py",
    }
    return reference, body


def test_fineract_health_is_up(fineract: FineractClient) -> None:
    assert fineract.health() is True


def test_fineract_generic_get_reads_gl_accounts(fineract: FineractClient) -> None:
    accounts = fineract.get("/glaccounts")
    assert isinstance(accounts, list)


def test_erp_lists_the_seeded_chart_parents_first(erp: ErpClient) -> None:
    accounts = erp.list_accounts(correlation_id="it-clients-list")
    assert len(accounts) >= 13
    seen: set[str] = set()
    for account in accounts:
        if account.parent_code is not None:
            assert account.parent_code in seen
        seen.add(account.code)


def test_unknown_erp_reference_is_none(fineract: FineractClient) -> None:
    assert fineract.get_erp_account(f"IT-NEVER-{secrets.token_hex(6)}") is None


def test_erp_unknown_journal_entry_is_none(erp: ErpClient) -> None:
    assert erp.get_journal_entry(f"IT-NEVER-{secrets.token_hex(6)}") is None


def test_sync_creates_then_unchanged_then_conflict(fineract: FineractClient) -> None:
    reference, body = unique_account()
    key = f"GL:{reference}"

    created = fineract.put_erp_account(reference, body, idempotency_key=key)
    assert created.outcome == "CREATED"
    assert created.erp_reference == reference
    assert created.gl_code == body["glCode"]

    replay = fineract.put_erp_account(reference, body, idempotency_key=key)
    assert replay == created

    again = fineract.put_erp_account(reference, body)
    assert again.outcome == "UNCHANGED"
    assert again.gl_account_id == created.gl_account_id

    mapping = fineract.get_erp_account(reference)
    assert mapping is not None
    assert mapping.gl_account_id == created.gl_account_id
    assert mapping.gl_code == body["glCode"]
    assert mapping.type == "ASSET"

    with pytest.raises(FineractConflict) as caught:
        fineract.put_erp_account(reference, {**body, "name": body["name"] + " changed"})
    assert caught.value.status == 409
    assert "error.msg.glaccount.sync.payload.conflict" in caught.value.error_codes


def test_bad_body_is_rejected_with_400(fineract: FineractClient) -> None:
    reference, body = unique_account()
    with pytest.raises(FineractRejected) as caught:
        fineract.put_erp_account(reference, {**body, "type": 6})
    assert caught.value.status == 400
    assert not isinstance(caught.value, FineractConflict)
