"""Setup steps against an in-memory fake Fineract: create when missing, leave alone when equal.

The fake answers the template and lookup calls with the shapes saved from Fineract 1.15 in
tests/fixtures/fineract_templates (offices_template.json, users_template.json,
currencies.json, permissions.json, office_by_external_id*.json, office_by_id.json)."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from integration_service.clients.errors import FineractNotFound
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
    find_office_by_external_id,
    permission_changes,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "fineract_templates"
GOOD_PASSWORD = "Int3gration-Svc!2026"
TODAY = date(2026, 10, 13)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class FakeFineract:
    """Just enough of Fineract for the setup steps. Records every call and every write.
    Template answers keep the saved fixture shapes, filled from the fake's own state."""

    def __init__(self) -> None:
        head = fixture("office_head.json")
        self.offices: list[dict[str, Any]] = [
            {
                "id": head["id"],
                "name": head["name"],
                "externalId": head["externalId"],
                "openingDate": date(*head["openingDate"]),
                "parentId": None,
            }
        ]
        currencies = fixture("currencies.json")
        self.currency_entries = {
            c["code"]: c
            for c in currencies["selectedCurrencyOptions"] + currencies["currencyOptions"]
        }
        self.currencies = ["USD"]
        self.permission_codes = [p["code"] for p in fixture("permissions.json")]
        self.roles: list[dict[str, Any]] = [{"id": 1, "name": "Super user", "disabled": False}]
        self.selected: dict[int, set[str]] = {1: {"ALL_FUNCTIONS"}}
        self.users: list[dict[str, Any]] = [
            {"id": 1, "username": "mifos", "officeId": 1, "selectedRoles": [{"id": 1}]}
        ]
        self.gets: list[str] = []
        self.writes: list[tuple[str, str, Any]] = []

    def add_office(
        self, office_id: int, name: str, parent_id: int, external_id: str | None = "OFF-KGL-MAIN"
    ) -> None:
        self.offices.append(
            {
                "id": office_id,
                "name": name,
                "externalId": external_id,
                "openingDate": date(2026, 1, 1),
                "parentId": parent_id,
            }
        )

    def _office_by_id(self, office: dict[str, Any]) -> dict[str, Any]:
        shown = {k: v for k, v in office.items() if v is not None}
        opened: date = office["openingDate"]
        shown["openingDate"] = [opened.year, opened.month, opened.day]
        if office["parentId"] is not None:
            parent = next(o for o in self.offices if o["id"] == office["parentId"])
            shown["parentName"] = parent["name"]
        return shown

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        self.gets.append(path)
        if path == "/offices":
            return [self._office_by_id(o) for o in self.offices]
        if path == "/offices/template":
            template = fixture("offices_template.json")
            template["allowedParents"] = [
                {"id": o["id"], "name": o["name"], "nameDecorated": o["name"]} for o in self.offices
            ]
            return template
        if path.startswith("/offices/external-id/"):
            external_id = path.rsplit("/", 1)[1]
            for office in self.offices:
                if office["externalId"] == external_id:
                    # This call shows no parentId, as on Fineract 1.15.
                    return {
                        "id": office["id"],
                        "name": office["name"],
                        "externalId": external_id,
                        "openingDate": office["openingDate"].isoformat(),
                    }
            raise FineractNotFound(404, fixture("office_by_external_id_404.json"))
        if path.startswith("/offices/"):
            office_id = int(path.split("/")[2])
            return self._office_by_id(next(o for o in self.offices if o["id"] == office_id))
        if path == "/currencies":
            return {
                "selectedCurrencyOptions": [self.currency_entries[c] for c in self.currencies],
                "currencyOptions": [
                    e for c, e in self.currency_entries.items() if c not in self.currencies
                ],
            }
        if path == "/permissions":
            return [{"code": c, "grouping": "x", "selected": False} for c in self.permission_codes]
        if path == "/roles":
            return self.roles
        if path.startswith("/roles/") and path.endswith("/permissions"):
            role_id = int(path.split("/")[2])
            return {
                "id": role_id,
                "permissionUsageData": [
                    {"code": code, "selected": code in self.selected[role_id]}
                    for code in self.permission_codes
                ],
            }
        if path == "/users/template":
            template = fixture("users_template.json")
            template["allowedOffices"] = [
                {"id": o["id"], "name": o["name"], "nameDecorated": o["name"]} for o in self.offices
            ]
            template["availableRoles"] = [dict(r) for r in self.roles]
            return template
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
            self.offices.append(
                {
                    "id": new_id,
                    "name": body["name"],
                    "externalId": body["externalId"],
                    "openingDate": date.fromisoformat(body["openingDate"]),
                    "parentId": body["parentId"],
                }
            )
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
    parent="Head Office",
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


def test_office_is_created_under_the_parent_the_template_names() -> None:
    fake = FakeFineract()
    assert ensure_office(fake, OFFICE, today=TODAY) == 2
    assert "/offices/template" in fake.gets
    assert fake.writes == [
        (
            "POST",
            "/offices",
            {
                "name": "Kigali Main Office",
                "externalId": "OFF-KGL-MAIN",
                "parentId": 1,
                "openingDate": "2026-01-01",
                "dateFormat": "yyyy-MM-dd",
                "locale": "en",
            },
        )
    ]


def test_parent_is_resolved_by_name_not_by_a_fixed_id() -> None:
    fake = FakeFineract()
    fake.add_office(5, "Rwanda Region", parent_id=1, external_id="OFF-RW")
    under_region = OFFICE.model_copy(update={"parent": "Rwanda Region"})
    ensure_office(fake, under_region, today=TODAY)
    assert fake.writes[0][2]["parentId"] == 5


def test_unknown_parent_is_refused_with_the_allowed_parents() -> None:
    fake = FakeFineract()
    bad = OFFICE.model_copy(update={"parent": "Nairobi Office"})
    with pytest.raises(SetupError, match=r"Nairobi Office.*Head Office"):
        ensure_office(fake, bad, today=TODAY)
    assert fake.writes == []


def test_opening_date_in_the_future_is_refused() -> None:
    fake = FakeFineract()
    later = OFFICE.model_copy(update={"opening_date": date(2026, 10, 14)})
    with pytest.raises(SetupError, match="future"):
        ensure_office(fake, later, today=TODAY)
    assert fake.writes == []


def test_opening_date_before_the_parent_opened_is_refused() -> None:
    fake = FakeFineract()  # Head Office opened on 2009-01-01 (office_head.json)
    early = OFFICE.model_copy(update={"opening_date": date(2008, 12, 31)})
    with pytest.raises(SetupError, match="2009-01-01"):
        ensure_office(fake, early, today=TODAY)
    assert fake.writes == []


def test_opening_date_on_the_parent_date_or_today_is_fine() -> None:
    for opening in (date(2009, 1, 1), TODAY):
        fake = FakeFineract()
        ensure_office(fake, OFFICE.model_copy(update={"opening_date": opening}), today=TODAY)
        assert fake.writes[0][2]["openingDate"] == opening.isoformat()


def test_existing_office_is_found_by_external_id_and_nothing_is_written() -> None:
    fake = FakeFineract()
    fake.add_office(7, "Kigali Main Office", parent_id=1)
    assert ensure_office(fake, OFFICE, today=TODAY) == 7
    assert ensure_office(fake, OFFICE, today=TODAY) == 7
    assert "/offices/external-id/OFF-KGL-MAIN" in fake.gets
    assert fake.writes == []


def test_find_office_by_external_id_reads_the_live_shapes() -> None:
    fake = FakeFineract()
    assert find_office_by_external_id(fake, "OFF-KGL-MAIN") is None  # the 404 fixture
    fake.add_office(7, "Kigali Main Office", parent_id=1)
    found = find_office_by_external_id(fake, "OFF-KGL-MAIN")
    assert found is not None
    assert found["id"] == 7


def test_office_with_our_external_id_under_another_parent_stops_setup() -> None:
    fake = FakeFineract()
    fake.add_office(3, "Rwanda Region", parent_id=1, external_id="OFF-RW")
    fake.add_office(7, "Kigali Main Office", parent_id=3)
    with pytest.raises(SetupError, match="Rwanda Region"):
        ensure_office(fake, OFFICE, today=TODAY)
    assert fake.writes == []


def test_office_with_our_external_id_and_another_name_stops_setup() -> None:
    fake = FakeFineract()
    fake.add_office(7, "Kigali Old Office", parent_id=1)
    with pytest.raises(SetupError, match="Kigali Old Office"):
        ensure_office(fake, OFFICE, today=TODAY)
    assert fake.writes == []


def test_office_with_our_name_but_another_external_id_stops_setup() -> None:
    fake = FakeFineract()
    fake.add_office(7, "Kigali Main Office", parent_id=1, external_id="OFF-OTHER")
    with pytest.raises(SetupError, match="OFF-OTHER"):
        ensure_office(fake, OFFICE, today=TODAY)
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


def test_currency_fineract_does_not_offer_is_refused_with_the_allowed_codes() -> None:
    fake = FakeFineract()
    with pytest.raises(SetupError, match=r"XYZ.*KES"):
        ensure_currency(fake, "XYZ")
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


def test_unknown_permission_code_is_refused_before_the_role_is_created() -> None:
    fake = FakeFineract()
    bad = ROLE.model_copy(update={"permissions": ["SYNC_GLACCOUNT", "READ_LOANTRANSACTION"]})
    with pytest.raises(UnknownPermissionsError, match="READ_LOANTRANSACTION"):
        ensure_role(fake, bad)
    assert "/permissions" in fake.gets
    assert fake.writes == []


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
    fake.add_office(2, "Kigali Main Office", parent_id=1)
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


def test_api_user_office_and_role_come_from_the_users_template() -> None:
    fake = FakeFineract()
    _with_office_and_role(fake)
    ensure_api_user(fake, USER, GOOD_PASSWORD)
    assert "/users/template" in fake.gets
    assert "/offices" not in fake.gets
    assert "/roles" not in fake.gets


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


def test_api_user_office_or_role_missing_from_the_template_is_refused_with_the_options() -> None:
    fake = FakeFineract()
    with pytest.raises(SetupError, match=r"Kigali Main Office.*Head Office"):
        ensure_api_user(fake, USER, GOOD_PASSWORD)
    fake.add_office(2, "Kigali Main Office", parent_id=1)
    with pytest.raises(SetupError, match=r"Integration Service.*Super user"):
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
    assert cfg.office.parent == "Head Office"
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


def _numeric_ids(value: Any, where: str = "") -> list[str]:
    """Every key that looks like a Fineract id, and every number, inside a JSON value."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "id" or (key.endswith("Id") and key != "externalId"):
                found.append(f"{where}.{key}")
            found += _numeric_ids(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for n, inner in enumerate(value):
            found += _numeric_ids(inner, f"{where}[{n}]")
    elif isinstance(value, int | float) and not isinstance(value, bool):
        found.append(where)
    return found


@pytest.mark.parametrize("name", ["office.json", "user.json", "currency.json", "role.json"])
def test_config_files_hold_no_fineract_ids(name: str) -> None:
    assert _numeric_ids(json.loads((REPO_CONFIG_DIR / name).read_text())) == []


def test_office_config_refuses_a_parent_id() -> None:
    with pytest.raises(ValidationError, match="parentId"):
        OfficeConfig.model_validate(
            {
                "name": "Kigali Main Office",
                "externalId": "OFF-KGL-MAIN",
                "parent": "Head Office",
                "parentId": 1,
                "openingDate": "2026-01-01",
            }
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
