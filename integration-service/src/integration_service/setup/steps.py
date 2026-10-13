"""Ensure steps for the office, currency, role and API user. Each one looks first, then
writes only what is missing or different. Setup uses Fineract's admin user.

The forms come from Fineract: GET /offices/template (the parents an office may have),
GET /users/template (the offices and roles a user may get), GET /currencies (the currencies
Fineract offers) and GET /permissions (the permission codes). The config files hold only our
choices, by name or code. A choice the form does not offer stops setup and lists the allowed
values.

What a re-run does when something already exists:
- office: found by externalId, never changed. Another name, parent or opening date stops setup.
- currency: added to the selected list when missing. The other selected ones are kept.
- role: its permissions are set to exactly the configured list. Extra ones are turned off.
- API user: office and roles are set back to the configured ones. The password is left alone,
  because Fineract cannot show it to compare."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

from integration_service.clients.errors import FineractNotFound
from integration_service.logs import log_event
from integration_service.services.templates import FINERACT_DATE_FORMAT, FINERACT_LOCALE

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


# --- template options ----------------------------------------------------------------------


def option_id(options: Iterable[dict[str, Any]], name: str, what: str) -> int:
    """The id of the template option with this name. SetupError naming the allowed ones."""
    choices = list(options)
    for option in choices:
        if option.get("name") == name:
            return int(option["id"])
    allowed = ", ".join(str(o.get("name")) for o in choices) or "none"
    raise SetupError(f"{what} {name!r} is not one Fineract offers. Allowed: {allowed}")


def fineract_date(value: Any) -> date:
    """Fineract shows a date as [2026, 1, 1] or as "2026-01-01", depending on the call."""
    if isinstance(value, list) and len(value) == 3:
        return date(int(value[0]), int(value[1]), int(value[2]))
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise SetupError(f"Unexpected date from Fineract: {value!r}")


# --- office --------------------------------------------------------------------------------


def find_office(fc: FineractApi, name: str) -> dict[str, Any] | None:
    for office in fc.get("/offices") or []:
        if office.get("name") == name:
            return dict(office)
    return None


def find_office_by_external_id(fc: FineractApi, external_id: str) -> dict[str, Any] | None:
    """GET /offices/external-id/{externalId}. None on Fineract's 404. This answer has no
    parentId, so read GET /offices/{id} for the parent."""
    try:
        office = fc.get(f"/offices/external-id/{quote(external_id, safe='')}")
    except FineractNotFound:
        return None
    return dict(office)


def check_opening_date(opening: date, parent: dict[str, Any], today: date) -> None:
    """Fineract 1.15 only needs an opening date. It does not check it against today or the
    parent (OfficeCommandFromApiJsonDeserializer, Office.fromJson). A client or loan in this
    office cannot be dated before the office opened, so a wrong date fails later and far
    from here. Setup refuses it now."""
    if opening > today:
        raise SetupError(f"Office opening date {opening} is in the future (today is {today})")
    parent_opening = fineract_date(parent["openingDate"])
    if opening < parent_opening:
        raise SetupError(
            f"Office opening date {opening} is before its parent {parent.get('name')!r} "
            f"opened on {parent_opening}"
        )


def ensure_office(fc: FineractApi, cfg: OfficeConfig, today: date | None = None) -> int:
    """The office's Fineract id, found by its externalId. When missing, it is created from
    GET /offices/template: the parent is one of the template's allowedParents, by name.
    An existing office is never changed; a different name, parent or opening date stops
    setup."""
    existing = find_office_by_external_id(fc, cfg.external_id)
    if existing is not None:
        office_id = int(existing["id"])
        office = fc.get(f"/offices/{office_id}")
        opened = fineract_date(office.get("openingDate"))
        if (office.get("name"), office.get("parentName"), opened) != (
            cfg.name,
            cfg.parent,
            cfg.opening_date,
        ):
            raise SetupError(
                f"Office {cfg.external_id} exists as {office.get('name')!r} under parent "
                f"{office.get('parentName')!r}, opened {opened}, not {cfg.name!r} under "
                f"{cfg.parent!r}, opened {cfg.opening_date}"
            )
        log_event("setup office exists", step="office", name=cfg.name)
        return office_id

    same_name = find_office(fc, cfg.name)
    if same_name is not None:
        raise SetupError(
            f"Office {cfg.name!r} exists with externalId {same_name.get('externalId')!r}, "
            f"not {cfg.external_id!r}"
        )

    template = fc.get("/offices/template")
    parent_id = option_id(template.get("allowedParents") or [], cfg.parent, "Parent office")
    # The template lists the parents without their opening dates.
    check_opening_date(cfg.opening_date, fc.get(f"/offices/{parent_id}"), today or date.today())
    created = fc.post(
        "/offices",
        {
            "name": cfg.name,
            "externalId": cfg.external_id,
            "parentId": parent_id,
            "openingDate": cfg.opening_date.isoformat(),
            "dateFormat": FINERACT_DATE_FORMAT,
            "locale": FINERACT_LOCALE,
        },
    )
    log_event("setup office created", step="office", name=cfg.name)
    return int(created["officeId"])


# --- currency ------------------------------------------------------------------------------


def ensure_currency(fc: FineractApi, code: str) -> bool:
    """Selects the currency. True when it was added. GET /currencies is the form: the
    selected codes plus the codes Fineract offers. PUT /currencies replaces the whole list,
    so the currencies already selected are sent again."""
    form = fc.get("/currencies")
    selected = [c["code"] for c in form.get("selectedCurrencyOptions") or []]
    if code in selected:
        log_event("setup currency exists", step="currency", code=code)
        return False
    offered = [c["code"] for c in form.get("currencyOptions") or []]
    if code not in offered:
        allowed = ", ".join(sorted({*selected, *offered}))
        raise SetupError(f"Currency {code!r} is not one Fineract offers. Allowed: {allowed}")
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


def check_permission_codes(fc: FineractApi, codes: Iterable[str]) -> None:
    """Every code must be one GET /permissions lists, so a typo stops setup before the role
    is created or changed."""
    known = {p["code"] for p in fc.get("/permissions") or []}
    unknown = sorted(set(codes) - known)
    if unknown:
        raise UnknownPermissionsError(unknown)


def ensure_role(fc: FineractApi, cfg: RoleConfig) -> int:
    """The role's Fineract id, holding exactly the configured permissions."""
    check_permission_codes(fc, cfg.permissions)
    role = find_role(fc, cfg.name)
    if role is None:
        created = fc.post("/roles", {"name": cfg.name, "description": cfg.description})
        role_id = int(created["resourceId"])
        log_event("setup role created", step="role", name=cfg.name)
    else:
        role_id = int(role["id"])
    _, held = role_permissions(fc, role_id)
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
    # GET /users/template is the form: the offices and roles this admin may give a user.
    template = fc.get("/users/template")
    office_id = option_id(template.get("allowedOffices") or [], cfg.office, "Office")
    role_id = option_id(template.get("availableRoles") or [], cfg.role, "Role")

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
