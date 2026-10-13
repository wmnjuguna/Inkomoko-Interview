"""Ensure steps for the office, currency, role and API user. Each one looks first, then
writes only what is missing or different. Setup uses Fineract's admin user.

What a re-run does when something already exists:
- office: found by name under its parent, never changed. Same name under another parent stops.
- currency: added to the selected list when missing. The other selected ones are kept.
- role: its permissions are set to exactly the configured list. Extra ones are turned off.
- API user: office and roles are set back to the configured ones. The password is left alone,
  because Fineract cannot show it to compare."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

from integration_service.logs import log_event

if TYPE_CHECKING:
    from integration_service.setup.config_files import ApiUserConfig, OfficeConfig, RoleConfig

# Fineract's active password policy (m_password_validation_policy id 3): 12 to 50 characters,
# upper and lower case, a digit, a special character, no spaces, no character twice in a row.
PASSWORD_POLICY = re.compile(
    r"^(?!.*(.)\1)(?!.*\s)(?=.*\d)(?=.*[a-z])(?=.*[A-Z])(?=.*[^\w\s]).{12,50}$"
)


class FineractApi(Protocol):
    """The part of FineractClient that setup uses. Tests pass a fake."""

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any: ...

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any: ...

    def put(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any: ...


class SetupError(Exception):
    """Setup cannot go on without a person looking at Fineract or the config."""


class UnknownPermissionsError(SetupError):
    """The role config names permission codes this Fineract does not have."""

    def __init__(self, codes: list[str]) -> None:
        self.codes = codes
        super().__init__(f"Fineract has no permission codes: {', '.join(codes)}")


class WeakPasswordError(SetupError):
    """The API user password does not meet Fineract's password policy."""


# --- office --------------------------------------------------------------------------------


def find_office(fc: FineractApi, name: str) -> dict[str, Any] | None:
    for office in fc.get("/offices") or []:
        if office.get("name") == name:
            return dict(office)
    return None


def ensure_office(fc: FineractApi, cfg: OfficeConfig) -> int:
    """The office's Fineract id. Creates it under its parent when missing."""
    office = find_office(fc, cfg.name)
    if office is not None:
        if office.get("parentId") != cfg.parent_id:
            raise SetupError(
                f"Office {cfg.name!r} exists under parent {office.get('parentId')}, "
                f"not {cfg.parent_id}"
            )
        log_event("setup office exists", step="office", name=cfg.name)
        return int(office["id"])
    created = fc.post(
        "/offices",
        {
            "name": cfg.name,
            "parentId": cfg.parent_id,
            "externalId": cfg.external_id,
            "openingDate": cfg.opening_date.isoformat(),
            "dateFormat": "yyyy-MM-dd",
            "locale": "en",
        },
    )
    log_event("setup office created", step="office", name=cfg.name)
    return int(created["officeId"])


# --- currency ------------------------------------------------------------------------------


def ensure_currency(fc: FineractApi, code: str) -> bool:
    """Selects the currency. True when it was added. PUT /currencies replaces the whole
    list, so the currencies already selected are sent again."""
    selected = [c["code"] for c in fc.get("/currencies").get("selectedCurrencyOptions") or []]
    if code in selected:
        log_event("setup currency exists", step="currency", code=code)
        return False
    fc.put("/currencies", {"currencies": [*selected, code]})
    log_event("setup currency added", step="currency", code=code)
    return True


# --- role ----------------------------------------------------------------------------------


def permission_changes(selected: Iterable[str], wanted: Iterable[str]) -> dict[str, bool]:
    """What to send so the role holds exactly the wanted codes: missing on, extra off."""
    have, want = set(selected), set(wanted)
    changes = {code: True for code in want - have} | {code: False for code in have - want}
    return dict(sorted(changes.items()))


def find_role(fc: FineractApi, name: str) -> dict[str, Any] | None:
    for role in fc.get("/roles") or []:
        if role.get("name") == name:
            return dict(role)
    return None


def role_permissions(fc: FineractApi, role_id: int) -> tuple[set[str], set[str]]:
    """(every permission code Fineract knows, the codes this role holds)."""
    usage = fc.get(f"/roles/{role_id}/permissions").get("permissionUsageData") or []
    known = {p["code"] for p in usage}
    held = {p["code"] for p in usage if p.get("selected")}
    return known, held


def ensure_role(fc: FineractApi, cfg: RoleConfig) -> int:
    """The role's Fineract id, holding exactly the configured permissions."""
    role = find_role(fc, cfg.name)
    if role is None:
        created = fc.post("/roles", {"name": cfg.name, "description": cfg.description})
        role_id = int(created["resourceId"])
        log_event("setup role created", step="role", name=cfg.name)
    else:
        role_id = int(role["id"])
    known, held = role_permissions(fc, role_id)
    unknown = sorted(set(cfg.permissions) - known)
    if unknown:
        raise UnknownPermissionsError(unknown)
    changes = permission_changes(held, cfg.permissions)
    if changes:
        fc.put(f"/roles/{role_id}/permissions", {"permissions": changes})
        log_event("setup role permissions set", step="role", name=cfg.name, changes=changes)
    else:
        log_event("setup role permissions equal", step="role", name=cfg.name)
    return role_id


# --- API user ------------------------------------------------------------------------------


def check_password(password: str) -> None:
    if not PASSWORD_POLICY.match(password):
        raise WeakPasswordError(
            "The API user password must be 12 to 50 characters with upper and lower case, "
            "a digit and a special character, no spaces, and no character twice in a row"
        )


def find_user(fc: FineractApi, username: str) -> dict[str, Any] | None:
    for user in fc.get("/users") or []:
        if user.get("username") == username:
            return dict(user)
    return None


def ensure_api_user(fc: FineractApi, cfg: ApiUserConfig, password: str) -> int:
    """The API user's Fineract id: in the configured office, with only the configured role."""
    check_password(password)
    office = find_office(fc, cfg.office)
    if office is None:
        raise SetupError(f"Office {cfg.office!r} not found; run ensure_office first")
    role = find_role(fc, cfg.role)
    if role is None:
        raise SetupError(f"Role {cfg.role!r} not found; run ensure_role first")
    office_id, role_id = int(office["id"]), int(role["id"])

    user = find_user(fc, cfg.username)
    if user is None:
        created = fc.post(
            "/users",
            {
                "username": cfg.username,
                "firstname": cfg.firstname,
                "lastname": cfg.lastname,
                "email": cfg.email,
                "officeId": office_id,
                "roles": [role_id],
                "sendPasswordToEmail": False,
                "password": password,
                "repeatPassword": password,
                "passwordNeverExpires": True,
            },
        )
        log_event("setup api user created", step="api_user", username=cfg.username)
        return int(created["resourceId"])

    user_id = int(user["id"])
    role_ids = {int(r["id"]) for r in user.get("selectedRoles") or []}
    if user.get("officeId") != office_id or role_ids != {role_id}:
        fc.put(f"/users/{user_id}", {"officeId": office_id, "roles": [role_id]})
        log_event("setup api user office and role set", step="api_user", username=cfg.username)
    else:
        log_event("setup api user exists", step="api_user", username=cfg.username)
    return user_id
