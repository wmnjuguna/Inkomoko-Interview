"""Setup steps against an in-memory fake Fineract: create when missing, leave alone when equal."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from integration_service.setup.config_files import (
    REPO_CONFIG_DIR,
    ApiUserConfig,
    OfficeConfig,
    RoleConfig,
    api_password_from_env,
    default_config_dir,
    load_setup_config,
)
from integration_service.setup.steps import (
    SetupError,
    UnknownPermissionsError,
    WeakPasswordError,
    check_password,
    ensure_api_user,
    ensure_currency,
    ensure_office,
    ensure_role,
    permission_changes,
)

GOOD_PASSWORD = "Int3gration-Svc!2026"
ALL_CODES = [
    "ALL_FUNCTIONS",
    "SYNC_GLACCOUNT",
    "READ_GLACCOUNT",
    "CREATE_CLIENT",
    "READ_CLIENT",
    "CREATE_LOAN",
    "DELETE_LOAN",
]


class FakeFineract:
    """Just enough of Fineract for the setup steps. Records every write."""

    def __init__(self) -> None:
        self.offices: list[dict[str, Any]] = [
            {"id": 1, "name": "Head Office", "externalId": "1", "hierarchy": "."}
        ]
        self.currencies = ["USD"]
        self.roles: list[dict[str, Any]] = [{"id": 1, "name": "Super user", "disabled": False}]
        self.selected: dict[int, set[str]] = {1: {"ALL_FUNCTIONS"}}
        self.users: list[dict[str, Any]] = [
            {"id": 1, "username": "mifos", "officeId": 1, "selectedRoles": [{"id": 1}]}
        ]
        self.writes: list[tuple[str, str, Any]] = []

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        if path == "/offices":
            return self.offices
        if path == "/currencies":
            return {
                "selectedCurrencyOptions": [{"code": c} for c in self.currencies],
                "currencyOptions": [{"code": c} for c in ("USD", "RWF", "KES")],
            }
        if path == "/roles":
            return self.roles
        if path.startswith("/roles/") and path.endswith("/permissions"):
            role_id = int(path.split("/")[2])
            return {
                "id": role_id,
                "permissionUsageData": [
                    {"code": code, "selected": code in self.selected[role_id]} for code in ALL_CODES
                ],
            }
        if path == "/users":
            return self.users
        raise AssertionError(f"unexpected GET {path}")

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.writes.append(("POST", path, body))
        if path == "/offices":
            new_id = len(self.offices) + 1
            self.offices.append({"id": new_id, "name": body["name"], "parentId": body["parentId"]})
            return {"officeId": new_id, "resourceId": new_id}
        if path == "/roles":
            new_id = len(self.roles) + 1
            self.roles.append({"id": new_id, "name": body["name"], "disabled": False})
            self.selected[new_id] = set()
            return {"resourceId": new_id}
        if path == "/users":
            new_id = len(self.users) + 1
            self.users.append(
                {
                    "id": new_id,
                    "username": body["username"],
                    "officeId": body["officeId"],
                    "selectedRoles": [{"id": r} for r in body["roles"]],
                }
            )
            return {"resourceId": new_id}
        raise AssertionError(f"unexpected POST {path}")

    def put(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.writes.append(("PUT", path, body))
        if path == "/currencies":
            self.currencies = list(body["currencies"])
            return {}
        if path.startswith("/roles/") and path.endswith("/permissions"):
            role_id = int(path.split("/")[2])
            for code, on in body["permissions"].items():
                (self.selected[role_id].add if on else self.selected[role_id].discard)(code)
            return {}
        if path.startswith("/users/"):
            user = next(u for u in self.users if u["id"] == int(path.split("/")[2]))
            user["officeId"] = body["officeId"]
            user["selectedRoles"] = [{"id": r} for r in body["roles"]]
            return {}
        raise AssertionError(f"unexpected PUT {path}")


OFFICE = OfficeConfig(
    name="Kigali Main Office",
    external_id="OFF-KGL-MAIN",
    parent_id=1,
    opening_date=date(2026, 1, 1),
)
ROLE = RoleConfig(
    name="Integration Service",
    description="Least privilege",
    permissions=["SYNC_GLACCOUNT", "READ_GLACCOUNT", "CREATE_CLIENT"],
)
USER = ApiUserConfig(
    username="integration-api",
    firstname="Integration",
    lastname="Service",
    email="integration-api@inkomoko.local",
    office="Kigali Main Office",
    role="Integration Service",
)


# --- office --------------------------------------------------------------------------------


def test_office_is_created_under_head_office_when_missing() -> None:
    fake = FakeFineract()
    assert ensure_office(fake, OFFICE) == 2
    assert fake.writes == [
        (
            "POST",
            "/offices",
            {
                "name": "Kigali Main Office",
                "parentId": 1,
                "externalId": "OFF-KGL-MAIN",
                "openingDate": "2026-01-01",
                "dateFormat": "yyyy-MM-dd",
                "locale": "en",
            },
        )
    ]


def test_existing_office_is_found_and_nothing_is_written() -> None:
    fake = FakeFineract()
    fake.offices.append({"id": 7, "name": "Kigali Main Office", "parentId": 1})
    assert ensure_office(fake, OFFICE) == 7
    assert ensure_office(fake, OFFICE) == 7
    assert fake.writes == []


def test_office_with_our_name_under_another_parent_stops_setup() -> None:
    fake = FakeFineract()
    fake.offices.append({"id": 7, "name": "Kigali Main Office", "parentId": 3})
    with pytest.raises(SetupError, match="parent 3"):
        ensure_office(fake, OFFICE)
    assert fake.writes == []


# --- currency ------------------------------------------------------------------------------


def test_currency_is_added_and_the_others_are_kept() -> None:
    fake = FakeFineract()
    assert ensure_currency(fake, "RWF") is True
    assert fake.writes == [("PUT", "/currencies", {"currencies": ["USD", "RWF"]})]


def test_currency_already_selected_writes_nothing() -> None:
    fake = FakeFineract()
    fake.currencies = ["USD", "RWF"]
    assert ensure_currency(fake, "RWF") is False
    assert fake.writes == []


# --- role ----------------------------------------------------------------------------------


def test_permission_changes_turn_on_missing_and_turn_off_extra() -> None:
    assert permission_changes({"A", "B", "X"}, ["A", "B", "C"]) == {"C": True, "X": False}
    assert permission_changes({"A", "B"}, ["B", "A"]) == {}
    assert permission_changes(set(), ["A"]) == {"A": True}


def test_role_is_created_with_exactly_the_configured_permissions() -> None:
    fake = FakeFineract()
    role_id = ensure_role(fake, ROLE)
    assert role_id == 2
    assert fake.writes == [
        ("POST", "/roles", {"name": "Integration Service", "description": "Least privilege"}),
        (
            "PUT",
            "/roles/2/permissions",
            {
                "permissions": {
                    "CREATE_CLIENT": True,
                    "READ_GLACCOUNT": True,
                    "SYNC_GLACCOUNT": True,
                }
            },
        ),
    ]
    assert fake.selected[2] == set(ROLE.permissions)


def test_role_rerun_with_the_same_permissions_writes_nothing() -> None:
    fake = FakeFineract()
    ensure_role(fake, ROLE)
    fake.writes.clear()
    assert ensure_role(fake, ROLE) == 2
    assert fake.writes == []


def test_role_with_other_permissions_is_set_back_to_the_configured_list() -> None:
    fake = FakeFineract()
    fake.roles.append({"id": 2, "name": "Integration Service", "disabled": False})
    fake.selected[2] = {"SYNC_GLACCOUNT", "DELETE_LOAN"}
    assert ensure_role(fake, ROLE) == 2
    assert fake.writes == [
        (
            "PUT",
            "/roles/2/permissions",
            {"permissions": {"CREATE_CLIENT": True, "DELETE_LOAN": False, "READ_GLACCOUNT": True}},
        )
    ]
    assert fake.selected[2] == set(ROLE.permissions)


def test_unknown_permission_code_stops_before_any_permission_write() -> None:
    fake = FakeFineract()
    fake.roles.append({"id": 2, "name": "Integration Service", "disabled": False})
    fake.selected[2] = set()
    bad = ROLE.model_copy(update={"permissions": ["SYNC_GLACCOUNT", "READ_LOANTRANSACTION"]})
    with pytest.raises(UnknownPermissionsError, match="READ_LOANTRANSACTION"):
        ensure_role(fake, bad)
    assert fake.writes == []


# --- API user ------------------------------------------------------------------------------


def _with_office_and_role(fake: FakeFineract) -> None:
    fake.offices.append({"id": 2, "name": "Kigali Main Office", "parentId": 1})
    fake.roles.append({"id": 5, "name": "Integration Service", "disabled": False})


def test_api_user_is_created_in_our_office_with_only_our_role() -> None:
    fake = FakeFineract()
    _with_office_and_role(fake)
    assert ensure_api_user(fake, USER, GOOD_PASSWORD) == 2
    assert fake.writes == [
        (
            "POST",
            "/users",
            {
                "username": "integration-api",
                "firstname": "Integration",
                "lastname": "Service",
                "email": "integration-api@inkomoko.local",
                "officeId": 2,
                "roles": [5],
                "sendPasswordToEmail": False,
                "password": GOOD_PASSWORD,
                "repeatPassword": GOOD_PASSWORD,
                "passwordNeverExpires": True,
            },
        )
    ]


def test_api_user_rerun_writes_nothing() -> None:
    fake = FakeFineract()
    _with_office_and_role(fake)
    ensure_api_user(fake, USER, GOOD_PASSWORD)
    fake.writes.clear()
    assert ensure_api_user(fake, USER, GOOD_PASSWORD) == 2
    assert fake.writes == []


@pytest.mark.parametrize(
    ("office_id", "role_ids"),
    [
        (1, [5]),  # wrong office, right role
        (2, [5, 1]),  # right office, an extra role
        (2, []),  # right office, no role
    ],
)
def test_api_user_with_other_office_or_roles_is_set_back_without_touching_the_password(
    office_id: int, role_ids: list[int]
) -> None:
    fake = FakeFineract()
    _with_office_and_role(fake)
    fake.users.append(
        {
            "id": 9,
            "username": "integration-api",
            "officeId": office_id,
            "selectedRoles": [{"id": r} for r in role_ids],
        }
    )
    assert ensure_api_user(fake, USER, GOOD_PASSWORD) == 9
    assert fake.writes == [("PUT", "/users/9", {"officeId": 2, "roles": [5]})]


def test_api_user_needs_the_office_and_role_first() -> None:
    fake = FakeFineract()
    with pytest.raises(SetupError, match="Kigali Main Office"):
        ensure_api_user(fake, USER, GOOD_PASSWORD)
    fake.offices.append({"id": 2, "name": "Kigali Main Office", "parentId": 1})
    with pytest.raises(SetupError, match="Integration Service"):
        ensure_api_user(fake, USER, GOOD_PASSWORD)
    assert fake.writes == []


@pytest.mark.parametrize(
    "password",
    [
        "change-me-integration",  # no upper case, no digit
        "Short1!a",  # under 12 characters
        "Int3gration-Svc!!2026",  # two same characters in a row
        "Int3gration Svc!2026",  # a space
        "Int3grationSvc2026",  # no special character
    ],
)
def test_weak_password_is_refused_before_anything_is_sent(password: str) -> None:
    with pytest.raises(WeakPasswordError):
        check_password(password)
    fake = FakeFineract()
    _with_office_and_role(fake)
    with pytest.raises(WeakPasswordError):
        ensure_api_user(fake, USER, password)
    assert fake.writes == []


def test_good_password_passes_the_policy() -> None:
    check_password(GOOD_PASSWORD)


# --- config files --------------------------------------------------------------------------


def test_repo_config_files_load() -> None:
    cfg = load_setup_config()
    assert cfg.office.name == "Kigali Main Office"
    assert cfg.office.parent_id == 1
    assert cfg.currency.code == "RWF"
    assert cfg.api_user.username == "integration-api"
    assert sorted(cfg.role.permissions) == sorted(
        [
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
    )


def test_config_dir_comes_from_env_when_set(tmp_path: Path) -> None:
    assert default_config_dir({}) == REPO_CONFIG_DIR
    assert default_config_dir({"FINERACT_SETUP_CONFIG_DIR": str(tmp_path)}) == tmp_path


def test_files_must_agree_on_office_and_role(tmp_path: Path) -> None:
    folder = tmp_path / "configuration"
    shutil.copytree(REPO_CONFIG_DIR, folder)
    user = json.loads((folder / "user.json").read_text())
    user["office"] = "Head Office"
    (folder / "user.json").write_text(json.dumps(user))
    with pytest.raises(ValidationError, match="office"):
        load_setup_config(folder)


def test_repeated_permission_is_refused() -> None:
    with pytest.raises(ValidationError, match="repeated"):
        RoleConfig(name="r", description="d", permissions=["A", "A"])


def test_api_password_comes_from_env() -> None:
    assert api_password_from_env({"FINERACT_API_PASSWORD": GOOD_PASSWORD}) == GOOD_PASSWORD
    with pytest.raises(RuntimeError, match="FINERACT_API_PASSWORD"):
        api_password_from_env({})
