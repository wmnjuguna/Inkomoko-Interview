"""The setup run against fakes: the order of the steps, the plain lines it prints, the
refusal to make the loan product before its GL accounts are synced, and the exit code."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from integration_service.clients.errors import FineractRejected, FineractUnavailable
from integration_service.clients.fineract import ErpAccountMapping
from integration_service.services.gl_sync import AccountStatus, SyncReport
from integration_service.setup import run as setup_run
from integration_service.setup.config_files import REPO_CONFIG_DIR, SetupConfig, load_setup_config
from integration_service.setup.product import REQUIRED_CASH_ROLES
from integration_service.setup.run import SetupEnv, main, run_setup, wait_until_ready
from integration_service.setup.steps import SetupError

PASSWORD = "Int3gration-Svc!2026"


def login_ok() -> None:
    """The check made as the API user: it worked."""


PERMISSIONS = [
    "SYNC_GLACCOUNT",
    "READ_GLACCOUNT",
    "CREATE_CLIENT",
    "ACTIVATE_CLIENT",
    "READ_CLIENT",
    "CREATE_LOAN",
    "APPROVE_LOAN",
    "DISBURSE_LOAN",
    "REPAYMENT_LOAN",
    "READ_LOAN",
    "READ_LOANPRODUCT",
]


class FakeFineract:
    """Enough of Fineract for a whole setup run. Records every call in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.offices: list[dict[str, Any]] = [{"id": 1, "name": "Head Office"}]
        self.currencies = ["USD"]
        self.roles: list[dict[str, Any]] = []
        self.selected: dict[int, set[str]] = {}
        self.users: list[dict[str, Any]] = []
        self.products: list[dict[str, Any]] = []
        self.gl_ids: dict[str, int] = {}

    def health(self) -> bool:
        return True

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        self.calls.append(f"GET {path}")
        if path == "/offices":
            return self.offices
        if path == "/currencies":
            return {"selectedCurrencyOptions": [{"code": c} for c in self.currencies]}
        if path == "/roles":
            return self.roles
        if path.startswith("/roles/"):
            role_id = int(path.split("/")[2])
            return {
                "permissionUsageData": [
                    {"code": c, "selected": c in self.selected[role_id]} for c in PERMISSIONS
                ]
            }
        if path == "/users":
            return self.users
        if path == "/loanproducts":
            return [{"id": p["id"], "name": p["name"]} for p in self.products]
        if path.startswith("/loanproducts/"):
            body = next(p["body"] for p in self.products if p["id"] == int(path.split("/")[2]))
            return _as_fineract_shows(body)
        raise AssertionError(f"unexpected GET {path}")

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.calls.append(f"POST {path}")
        if path == "/offices":
            self.offices.append({"id": 2, "name": body["name"], "parentId": body["parentId"]})
            return {"officeId": 2}
        if path == "/roles":
            self.roles.append({"id": 3, "name": body["name"]})
            self.selected[3] = set()
            return {"resourceId": 3}
        if path == "/users":
            self.users.append(
                {
                    "id": 4,
                    "username": body["username"],
                    "officeId": body["officeId"],
                    "selectedRoles": [{"id": r} for r in body["roles"]],
                }
            )
            return {"resourceId": 4}
        if path == "/loanproducts":
            self.products.append({"id": 9, "name": body["name"], "body": body})
            return {"resourceId": 9}
        raise AssertionError(f"unexpected POST {path}")

    def put(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.calls.append(f"PUT {path}")
        if path == "/currencies":
            self.currencies = list(body["currencies"])
        elif path.startswith("/roles/"):
            role_id = int(path.split("/")[2])
            self.selected[role_id] |= {c for c, on in body["permissions"].items() if on}
        else:
            raise AssertionError(f"unexpected PUT {path}")
        return {}

    def get_erp_account(
        self, erp_reference: str, correlation_id: str | None = None
    ) -> ErpAccountMapping | None:
        self.calls.append(f"LOOKUP {erp_reference}")
        gl_id = self.gl_ids.get(erp_reference)
        if gl_id is None:
            return None
        return ErpAccountMapping(
            erp_reference=erp_reference,
            gl_account_id=gl_id,
            gl_code=erp_reference.removeprefix("GL-"),
            name="x",
            type="ASSET",
            usage="DETAIL",
            parent_account=None,
            hierarchy=None,
            synced_at=datetime.now(UTC),
        )


def _as_fineract_shows(body: dict[str, Any]) -> dict[str, Any]:
    """GET /loanproducts/{id} for a body Fineract accepted (the parts setup compares)."""
    options = (
        "repaymentFrequencyType",
        "interestRateFrequencyType",
        "amortizationType",
        "interestType",
        "interestCalculationPeriodType",
        "accountingRule",
    )
    plain = (
        "shortName",
        "principal",
        "minPrincipal",
        "maxPrincipal",
        "numberOfRepayments",
        "repaymentEvery",
        "interestRatePerPeriod",
        "transactionProcessingStrategyCode",
        "multiDisburseLoan",
    )
    return {
        **{key: body[key] for key in plain},
        **{key: {"id": body[key]} for key in options},
        "currency": {
            "code": body["currencyCode"],
            "decimalPlaces": body["digitsAfterDecimal"],
            "inMultiplesOf": body["inMultiplesOf"],
        },
        "accountingMappings": {
            role.removesuffix("Id"): {"id": body[role]} for role in REQUIRED_CASH_ROLES
        },
    }


@pytest.fixture
def cfg() -> SetupConfig:
    return load_setup_config(REPO_CONFIG_DIR)


def _status(reference: str, status: str = "SYNCED", error: str | None = None) -> AccountStatus:
    return AccountStatus(reference, reference.removeprefix("GL-"), "x", status, 1, error)


def _synced_chart(fake: FakeFineract, cfg: SetupConfig, **odd: str) -> Callable[[], SyncReport]:
    """A sync that marks every account the product needs as SYNCED (unless named in odd)."""
    references = ["GL-1000", *cfg.loan_product.gl_accounts.values()]

    def sync() -> SyncReport:
        fake.calls.append("GL SYNC")
        for n, ref in enumerate(references, start=50):
            if ref not in odd:
                fake.gl_ids[ref] = n
        return SyncReport(
            [_status(ref, odd.get(ref, "SYNCED")) for ref in references], writes=len(references)
        )

    return sync


def _kinds(calls: list[str]) -> list[str]:
    """The first call of each step, so the test reads as the step order."""
    marks = {
        "GET /offices": "office",
        "GET /currencies": "currency",
        "GET /roles": "role",
        "GET /users": "user",
        "GL SYNC": "gl sync",
        "LOOKUP": "lookup",
        "GET /loanproducts": "product",
    }
    order: list[str] = []
    for call in calls:
        for prefix, kind in marks.items():
            if call.startswith(prefix) and kind not in order:
                order.append(kind)
    return order


def test_a_fresh_fineract_is_set_up_in_order_with_one_line_per_step(cfg: SetupConfig) -> None:
    fake = FakeFineract()
    lines: list[str] = []
    ready = {"fineract": lambda: True, "erp": lambda: True}

    product_id = run_setup(
        fake, cfg, PASSWORD, _synced_chart(fake, cfg), ready, api_login=login_ok, say=lines.append
    )

    assert product_id == 9
    assert _kinds(fake.calls) == [
        "office",
        "currency",
        "role",
        "user",
        "gl sync",
        "lookup",
        "product",
    ]
    assert lines == [
        "fineract: up",
        "erp: up",
        "office Kigali Main Office: created",
        "currency RWF: added",
        "role Integration Service: created",
        "api user integration-api: created",
        "api user integration-api: login ok",
        "gl sync: 10 SYNCED, 0 SYNC_PENDING, 0 FAILED (10 sent to Fineract)",
        "gl accounts for the loan product: all 9 synced",
        "loan product SME Asset Finance: created",
        "setup done",
    ]
    assert "POST /loanproducts" in fake.calls


def test_a_second_run_finds_everything_and_writes_nothing_new(cfg: SetupConfig) -> None:
    fake = FakeFineract()
    ready = {"fineract": lambda: True, "erp": lambda: True}
    run_setup(
        fake, cfg, PASSWORD, _synced_chart(fake, cfg), ready, api_login=login_ok, say=lambda _: None
    )
    fake.calls.clear()
    lines: list[str] = []

    run_setup(
        fake, cfg, PASSWORD, _synced_chart(fake, cfg), ready, api_login=login_ok, say=lines.append
    )

    assert [c for c in fake.calls if c.startswith(("POST", "PUT"))] == []
    assert "office Kigali Main Office: exists" in lines
    assert "currency RWF: exists" in lines
    assert "role Integration Service: exists" in lines
    assert "api user integration-api: exists" in lines
    assert "loan product SME Asset Finance: exists" in lines


@pytest.mark.parametrize("status", ["SYNC_PENDING", "FAILED"])
def test_product_is_refused_unless_every_account_it_needs_is_synced(
    cfg: SetupConfig, status: str
) -> None:
    fake = FakeFineract()
    ready = {"fineract": lambda: True, "erp": lambda: True}
    sync = _synced_chart(fake, cfg, **{"GL-4300": status})

    with pytest.raises(SetupError, match=rf"GL-4300 \({status}"):
        run_setup(fake, cfg, PASSWORD, sync, ready, api_login=login_ok, say=lambda _: None)

    assert not [c for c in fake.calls if "loanproducts" in c]


def test_product_is_refused_when_an_account_it_needs_is_missing_from_the_report(
    cfg: SetupConfig,
) -> None:
    fake = FakeFineract()
    ready = {"fineract": lambda: True, "erp": lambda: True}

    def empty_sync() -> SyncReport:
        return SyncReport([])

    with pytest.raises(SetupError, match="GL-1100 \\(not in the ERP chart"):
        run_setup(fake, cfg, PASSWORD, empty_sync, ready, api_login=login_ok, say=lambda _: None)
    assert not [c for c in fake.calls if "loanproducts" in c]


def test_nothing_is_written_when_a_service_never_comes_up(cfg: SetupConfig) -> None:
    fake = FakeFineract()
    ready = {"fineract": lambda: True, "erp": lambda: False}

    with pytest.raises(SetupError, match="erp is not up after 0s"):
        run_setup(
            fake,
            cfg,
            PASSWORD,
            _synced_chart(fake, cfg),
            ready,
            api_login=login_ok,
            say=lambda _: None,
            wait_seconds=0,
        )
    assert fake.calls == []


def test_wait_until_ready_polls_until_up() -> None:
    answers = iter([False, False, True])
    slept: list[float] = []
    now = [0.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    lines: list[str] = []
    wait_until_ready(
        {"fineract": lambda: next(answers)},
        timeout_seconds=60,
        say=lines.append,
        sleep=sleep,
        clock=lambda: now[0],
    )
    assert slept == [5, 5]
    assert lines == ["fineract: up"]


ENV = {
    "INTEGRATION_DATABASE_URL": "postgresql+psycopg://u:p@localhost:1/nothing",
    "FINERACT_BASE_URL": "http://localhost:1/fineract-provider/api/v1",
    "FINERACT_ADMIN_USERNAME": "mifos",
    "FINERACT_ADMIN_PASSWORD": "password",
    "FINERACT_API_USERNAME": "integration-api",
    "FINERACT_API_PASSWORD": PASSWORD,
    "ERP_BASE_URL": "http://localhost:1",
    "ERP_API_KEY": "k",
}


def test_env_is_read_with_defaults() -> None:
    env = SetupEnv.from_env(ENV)
    assert env.admin_username == "mifos"
    assert env.fineract_tenant == "default"
    assert env.wait_seconds == 600


def test_main_exits_non_zero_with_a_clear_message_when_env_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {k: v for k, v in ENV.items() if k != "FINERACT_ADMIN_PASSWORD"}
    assert main(env) == 1
    assert "setup failed: FINERACT_ADMIN_PASSWORD is not set" in capsys.readouterr().err


def test_main_refuses_an_api_username_other_than_user_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(ENV | {"FINERACT_API_USERNAME": "someone-else"}) == 1
    assert "user.json" in capsys.readouterr().err


def test_main_turns_an_upstream_failure_into_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_: Any, **__: Any) -> int:
        raise FineractUnavailable("connection refused")

    monkeypatch.setattr(setup_run, "run_setup", boom)
    assert main(ENV) == 1
    assert "setup failed: connection refused" in capsys.readouterr().err


def test_blank_numbers_use_the_defaults() -> None:
    env = SetupEnv.from_env(
        ENV | {"FINERACT_TIMEOUT_SECONDS": "", "ERP_TIMEOUT_SECONDS": " ", "SETUP_WAIT_SECONDS": ""}
    )
    assert env.fineract_timeout_seconds == 30
    assert env.erp_timeout_seconds == 10
    assert env.wait_seconds == 600


def test_a_bad_number_names_the_variable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(ENV | {"FINERACT_TIMEOUT_SECONDS": "thirty"}) == 1
    assert (
        "setup failed: FINERACT_TIMEOUT_SECONDS must be a number of seconds"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("status", "hint"),
    [(401, "FINERACT_API_PASSWORD"), (403, "role Integration Service")],
)
def test_setup_stops_when_the_api_user_cannot_work(
    cfg: SetupConfig, status: int, hint: str
) -> None:
    # The user already exists with another password (an old Fineract volume, a changed
    # .env), or its role lacks a permission. The service would fail on every call.
    fake = FakeFineract()
    ready = {"fineract": lambda: True, "erp": lambda: True}

    def refused() -> None:
        raise FineractRejected(status, {"userMessageGlobalisationCode": "error.msg.denied"})

    with pytest.raises(SetupError, match=hint):
        run_setup(
            fake,
            cfg,
            PASSWORD,
            _synced_chart(fake, cfg),
            ready,
            api_login=refused,
            say=lambda _: None,
        )
    assert "GL SYNC" not in fake.calls
    assert not [c for c in fake.calls if "loanproducts" in c]
