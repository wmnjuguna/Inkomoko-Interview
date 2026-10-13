"""One setup run: python -m integration_service.setup

The order:
1. Wait until Fineract and the ERP answer their health checks.
2. Office, currency, role and API user (each one looks first, so a re-run is safe).
   Then one read as the API user itself. A user that already exists keeps its password,
   so this catches a .env password that no longer matches Fineract (401) or a role that
   lacks a permission (403) before the service starts failing on every call.
3. GL sync of the whole ERP chart, through the same service the API uses. It records
   its rows in integration_db, so the Alembic migration must have run first (the
   container entrypoint does that).
4. Stop unless every ERP account the loan product maps is SYNCED.
5. The loan product.

It prints one plain line per step and exits 1 with "setup failed: ..." on any failure.
Setup logs in as Fineract's admin. The service itself later uses the API user."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import ValidationError

from integration_service.clients.erp import ErpClient
from integration_service.clients.errors import FineractRejected, UpstreamError
from integration_service.clients.fineract import ErpAccountMapping, FineractClient
from integration_service.config import env_seconds
from integration_service.db import make_engine
from integration_service.services.gl_sync import SYNCED, SyncReport, sync_all
from integration_service.setup.config_files import (
    API_PASSWORD_ENV,
    CONFIG_DIR_ENV,
    SetupConfig,
    load_setup_config,
)
from integration_service.setup.product import (
    build_product_body,
    ensure_product,
    fetch_product_template,
)
from integration_service.setup.steps import (
    FineractApi,
    SetupError,
    ensure_api_user,
    ensure_currency,
    ensure_office,
    ensure_role,
    find_office_by_external_id,
    find_role,
    find_user,
)

POLL_SECONDS = 5.0


class SetupFineract(FineractApi, Protocol):
    """What setup needs from Fineract. FineractClient fits."""

    def get_erp_account(
        self, erp_reference: str, correlation_id: str | None = None
    ) -> ErpAccountMapping | None: ...


@dataclass(frozen=True)
class SetupEnv:
    """Setup's own settings. It uses the admin user, not the service's API user."""

    database_url: str
    fineract_base_url: str
    admin_username: str
    admin_password: str
    api_username: str
    api_password: str
    erp_base_url: str
    erp_api_key: str
    fineract_tenant: str = "default"
    fineract_timeout_seconds: float = 30.0
    erp_timeout_seconds: float = 10.0
    wait_seconds: float = 600.0
    config_dir: Path | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> SetupEnv:
        def required(name: str) -> str:
            value = environ.get(name, "").strip()
            if not value:
                raise SetupError(f"{name} is not set")
            return value

        def seconds(name: str, default: float) -> float:
            try:
                return env_seconds(environ, name, default)
            except RuntimeError as exc:
                raise SetupError(str(exc)) from exc

        config_dir = environ.get(CONFIG_DIR_ENV, "").strip()
        return cls(
            database_url=required("INTEGRATION_DATABASE_URL"),
            fineract_base_url=required("FINERACT_BASE_URL").rstrip("/"),
            admin_username=required("FINERACT_ADMIN_USERNAME"),
            admin_password=required("FINERACT_ADMIN_PASSWORD"),
            api_username=required("FINERACT_API_USERNAME"),
            api_password=required(API_PASSWORD_ENV),
            erp_base_url=required("ERP_BASE_URL").rstrip("/"),
            erp_api_key=required("ERP_API_KEY"),
            fineract_tenant=environ.get("FINERACT_TENANT", "").strip() or "default",
            fineract_timeout_seconds=seconds("FINERACT_TIMEOUT_SECONDS", 30.0),
            erp_timeout_seconds=seconds("ERP_TIMEOUT_SECONDS", 10.0),
            wait_seconds=seconds("SETUP_WAIT_SECONDS", 600.0),
            config_dir=Path(config_dir) if config_dir else None,
        )


def wait_until_ready(
    checks: Mapping[str, Callable[[], bool]],
    timeout_seconds: float,
    say: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Poll each health check until it says up. SetupError after timeout_seconds."""
    deadline = clock() + timeout_seconds
    for name, is_up in checks.items():
        while not is_up():
            if clock() >= deadline:
                raise SetupError(f"{name} is not up after {timeout_seconds:.0f}s")
            sleep(POLL_SECONDS)
        say(f"{name}: up")


def run_setup(
    fc: SetupFineract,
    cfg: SetupConfig,
    api_password: str,
    sync_chart: Callable[[], SyncReport],
    ready: Mapping[str, Callable[[], bool]],
    *,
    api_login: Callable[[], object],
    say: Callable[[str], None] = print,
    wait_seconds: float = 600.0,
) -> int:
    """Run every step in order. Returns the loan product's Fineract id.
    api_login makes one read as the API user (not as admin)."""
    wait_until_ready(ready, wait_seconds, say)

    office_found = find_office_by_external_id(fc, cfg.office.external_id) is not None
    ensure_office(fc, cfg.office)
    say(f"office {cfg.office.name}: {'exists' if office_found else 'created'}")

    added = ensure_currency(fc, cfg.currency.code)
    say(f"currency {cfg.currency.code}: {'added' if added else 'exists'}")

    role_found = find_role(fc, cfg.role.name) is not None
    ensure_role(fc, cfg.role)
    say(f"role {cfg.role.name}: {'exists' if role_found else 'created'}")

    user_found = find_user(fc, cfg.api_user.username) is not None
    ensure_api_user(fc, cfg.api_user, api_password)
    say(f"api user {cfg.api_user.username}: {'exists' if user_found else 'created'}")
    _check_api_login(api_login, cfg)
    say(f"api user {cfg.api_user.username}: login ok")

    report = sync_chart()
    counts = report.counts
    say(
        f"gl sync: {counts['SYNCED']} SYNCED, {counts['SYNC_PENDING']} SYNC_PENDING, "
        f"{counts['FAILED']} FAILED ({report.writes} sent to Fineract)"
    )

    needed = list(dict.fromkeys(cfg.loan_product.gl_accounts.values()))
    _refuse_unless_synced(report, needed)
    say(f"gl accounts for the loan product: all {len(needed)} synced")

    gl_ids = {}
    for reference in needed:
        mapping = fc.get_erp_account(reference)
        if mapping is not None:
            gl_ids[reference] = mapping.gl_account_id
    body = build_product_body(cfg.loan_product, gl_ids, fetch_product_template(fc))
    product_found = any(p.get("name") == body["name"] for p in fc.get("/loanproducts") or [])
    product_id = ensure_product(fc, body)
    say(f"loan product {body['name']}: {'exists' if product_found else 'created'}")
    say("setup done")
    return product_id


def _check_api_login(api_login: Callable[[], object], cfg: SetupConfig) -> None:
    username = cfg.api_user.username
    try:
        api_login()
    except FineractRejected as exc:
        if exc.status == 401:
            raise SetupError(
                f"Fineract refused the login of {username} (401). The user exists with "
                "another password than FINERACT_API_PASSWORD. Setup never changes the "
                "password of an existing user: reset it in Fineract to the .env value, "
                "or set FINERACT_API_PASSWORD to the one Fineract holds."
            ) from exc
        if exc.status == 403:
            raise SetupError(
                f"{username} can log in but may not read GL mappings (403). Check the "
                f"permissions of role {cfg.role.name}."
            ) from exc
        raise


def _refuse_unless_synced(report: SyncReport, needed: list[str]) -> None:
    by_reference = {a.erp_reference: a for a in report.accounts}
    problems = []
    for reference in needed:
        account = by_reference.get(reference)
        if account is None:
            problems.append(f"{reference} (not in the ERP chart)")
        elif account.status != SYNCED:
            reason = f": {account.last_error}" if account.last_error else ""
            problems.append(f"{reference} ({account.status}{reason})")
    if problems:
        raise SetupError(
            "The loan product needs these ERP accounts SYNCED first: "
            + "; ".join(problems)
            + ". Fix them and run setup again."
        )


def _erp_is_up(base_url: str) -> Callable[[], bool]:
    def check() -> bool:
        try:
            return httpx.get(f"{base_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    return check


def _failure(exc: Exception) -> str:
    if isinstance(exc, FineractRejected) and exc.error_codes:
        return f"{exc.message} ({', '.join(exc.error_codes)})"
    if isinstance(exc, UpstreamError):
        return exc.message
    return str(exc)


def main(environ: Mapping[str, str] = os.environ) -> int:
    """Run setup from environment variables. 0 when done, 1 on any failure."""
    try:
        env = SetupEnv.from_env(environ)
        cfg = load_setup_config(env.config_dir)
        if cfg.api_user.username != env.api_username:
            raise SetupError(
                f"FINERACT_API_USERNAME is {env.api_username!r} but user.json says "
                f"{cfg.api_user.username!r}; they must match"
            )
        engine = make_engine(env.database_url)
        fc = FineractClient(
            env.fineract_base_url,
            env.fineract_tenant,
            env.admin_username,
            env.admin_password,
            env.fineract_timeout_seconds,
        )
        # The service's own user: only used for the login check.
        api_fc = FineractClient(
            env.fineract_base_url,
            env.fineract_tenant,
            env.api_username,
            env.api_password,
            env.fineract_timeout_seconds,
        )
        # Any account the product needs: a 404 (not synced yet) is fine, 401/403 is not.
        probe_reference = next(iter(cfg.loan_product.gl_accounts.values()))
        erp = ErpClient(env.erp_base_url, env.erp_api_key, env.erp_timeout_seconds)
        try:
            run_setup(
                fc,
                cfg,
                env.api_password,
                lambda: sync_all(engine, erp, fc),
                {"fineract": fc.health, "erp": _erp_is_up(env.erp_base_url)},
                api_login=lambda: api_fc.get_erp_account(probe_reference),
                wait_seconds=env.wait_seconds,
            )
        finally:
            fc.close()
            api_fc.close()
            erp.close()
            engine.dispose()
    except (SetupError, UpstreamError, ValidationError, OSError, ValueError) as exc:
        print(f"setup failed: {_failure(exc)}", file=sys.stderr)
        return 1
    return 0
