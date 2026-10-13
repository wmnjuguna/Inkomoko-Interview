"""The least-privilege Fineract role, used for real against the running Fineract.

The other live tests log in as admin. These log in as a user that holds only the
permissions in fineract/configuration/role.json, so they show that this role is enough for
GL sync, and that it is refused an admin call.

Fineract is shared, so the tests use their own copy: role "IT Integration Service copy"
and user "it-integration-copy" in Head Office. They are made once (through the real
ensure_role and ensure_api_user) and reused on later runs; nothing is deleted. The role's
permissions are set back to role.json on every run.

The last test logs in as the real service user (FINERACT_API_USERNAME). It is skipped,
with the reason, until setup has created that user."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from integration_service.clients.erp import ErpAccount
from integration_service.clients.errors import FineractRejected
from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.mapping.account_types import build_fineract_gl_body
from integration_service.services.gl_sync import sync_all
from integration_service.setup.config_files import ApiUserConfig, RoleConfig, load_setup_config
from integration_service.setup.steps import ensure_api_user, ensure_role

COPY_ROLE = "IT Integration Service copy"
COPY_USER = "it-integration-copy"
COPY_PASSWORD = "It-Integration-Copy-2026!"


def client_as(settings: Settings, username: str, password: str) -> FineractClient:
    return FineractClient(
        base_url=settings.fineract_base_url,
        tenant=settings.fineract_tenant,
        username=username,
        password=password,
        timeout_seconds=30,
    )


@pytest.fixture
def least_privilege(settings: Settings) -> Iterator[FineractClient]:
    """A user with exactly the role.json permissions, made with the admin user."""
    role = load_setup_config().role
    admin = client_as(settings, settings.fineract_username, settings.fineract_password)
    try:
        ensure_role(
            admin,
            RoleConfig(name=COPY_ROLE, description=role.description, permissions=role.permissions),
        )
        ensure_api_user(
            admin,
            ApiUserConfig(
                username=COPY_USER,
                firstname="Integration",
                lastname="Copy",
                email="it-integration-copy@inkomoko.local",
                office="Head Office",
                role=COPY_ROLE,
            ),
            COPY_PASSWORD,
        )
    finally:
        admin.close()
    client = client_as(settings, COPY_USER, COPY_PASSWORD)
    yield client
    client.close()


def fresh_account() -> ErpAccount:
    nonce = f"{secrets.randbelow(10**7):07d}"
    return ErpAccount(
        code=f"7{nonce}",
        external_reference=f"IT-LP-{nonce}",
        name=f"Least privilege test {nonce}",
        type="EXPENSE",
        usage="DETAIL",
        parent_code=None,
        currency="RWF",
        created_at=datetime.now(UTC),
    )


class OneAccountErp:
    def __init__(self, account: ErpAccount) -> None:
        self.account = account

    def list_accounts(self, correlation_id: str | None = None) -> list[ErpAccount]:
        return [self.account]


def test_the_role_can_sync_a_new_gl_account(least_privilege: FineractClient, db: Engine) -> None:
    account = fresh_account()

    report = sync_all(db, OneAccountErp(account), least_privilege)

    assert report.counts["SYNCED"] == 1, report
    mapping = least_privilege.get_erp_account(account.external_reference)
    assert mapping is not None and mapping.gl_code == account.code
    again = least_privilege.put_erp_account(
        account.external_reference, build_fineract_gl_body(account)
    )
    assert again.outcome == "UNCHANGED"


# GET /glaccounts is allowed: READ_GLACCOUNT (needed for the plugin GET) covers it too.
@pytest.mark.parametrize("path", ["/users", "/roles", "/offices", "/codes"])
def test_the_role_is_refused_admin_reads(least_privilege: FineractClient, path: str) -> None:
    with pytest.raises(FineractRejected) as caught:
        least_privilege.get(path)
    assert caught.value.status == 403


def test_the_role_cannot_create_a_gl_account_outside_the_plugin(
    least_privilege: FineractClient,
) -> None:
    account = fresh_account()
    body = {
        "name": account.name,
        "glCode": account.code,
        "type": 5,
        "usage": 1,
        "manualEntriesAllowed": True,
    }
    with pytest.raises(FineractRejected) as caught:
        least_privilege.post("/glaccounts", body)
    assert caught.value.status == 403


def test_the_real_service_user_can_read_gl_mappings(settings: Settings) -> None:
    username = os.environ.get("FINERACT_API_USERNAME", "integration-api")
    password = os.environ.get("FINERACT_API_PASSWORD", "Change-Me-Fineract-Api-1")
    client = client_as(settings, username, password)
    try:
        client.get_erp_account("GL-1200")
    except FineractRejected as exc:
        if exc.status == 401:
            pytest.skip(f"{username} cannot log in yet (401): run setup first")
        raise
    finally:
        client.close()
