"""Server-rendered pages under /ui: Jinja2 templates, HTMX for the live parts.

/ui is open (no API key) because the ERP only listens on localhost. As light CSRF
protection, every POST must carry the HX-Request header that HTMX sends. A plain
cross-site form post cannot set that header, so it is refused with 400.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from flask import Blueprint, Response, current_app, redirect, render_template, request, url_for
from flask.blueprints import BlueprintSetupState
from pydantic import ValidationError
from werkzeug.wrappers import Response as BaseResponse

from mock_erp import faults
from mock_erp.db import get_engine
from mock_erp.errors import ErpError
from mock_erp.schemas.accounts import Account, CreateAccountRequest
from mock_erp.schemas.faults import FaultSettingIn, FaultSettingOut
from mock_erp.services import accounts as account_service
from mock_erp.ui import queries

bp = Blueprint(
    "ui", __name__, url_prefix="/ui", template_folder="templates", static_folder="static"
)

ACCOUNT_FIELDS = ("code", "external_reference", "name", "type", "usage", "parent_code")
FAULT_FIELDS = ("mode", "path_prefix", "delay_seconds", "remaining", "retry_after")
ACTIVITY_SIZE = 8


@dataclass(frozen=True)
class FaultTile:
    mode: str
    badge: str
    label: str
    what: str


FAULT_TILES = (
    FaultTile(
        "down",
        "503",
        "ERP down",
        "Answers at once and saves nothing. The caller keeps the entry and tries again.",
    ),
    FaultTile(
        "timeout",
        "504",
        "Slow, nothing saved",
        "Waits, saves nothing, then gives up. The caller can't tell, so it retries "
        "with the same reference.",
    ),
    FaultTile(
        "timeout_after_write",
        "201",
        "Slow after saving",
        "Saves the entry, then answers too late. The retry gets 200 and exactly one entry exists.",
    ),
    FaultTile(
        "rate_limited",
        "429",
        "Rate limited",
        "Turns callers away for a while. A good caller waits as long as Retry-After says.",
    ),
)
FAULT_LABELS = {tile.mode: tile.label for tile in FAULT_TILES}


# Root redirect. Registered on the app when this blueprint is, so app.py needs no change.


def _root() -> BaseResponse:
    return redirect(url_for("ui.accounts_page"))


@bp.record_once
def _add_root_route(state: BlueprintSetupState) -> None:
    state.app.add_url_rule("/", endpoint="ui_root", view_func=_root)


# Light CSRF guard and helpers


@bp.before_request
def require_htmx_on_post() -> Response | None:
    if request.method == "POST" and request.headers.get("HX-Request") != "true":
        return Response(
            "This form only works from the page (missing HX-Request header).",
            status=400,
            mimetype="text/plain",
        )
    return None


@bp.app_template_filter("rwf")
def format_rwf(value: int) -> str:
    """Whole RWF with thousands separators; negatives in brackets, as accountants write."""
    text = f"{abs(value):,}"
    return f"({text})" if value < 0 else text


def with_toast(response: Response, message: str, kind: str = "good") -> Response:
    """HTMX fires a `toast` event from this header; app.js shows it."""
    response.headers["HX-Trigger"] = json.dumps({"toast": {"message": message, "kind": kind}})
    return response


def html(body: str, status: int = 200) -> Response:
    return Response(body, status=status, mimetype="text/html")


def first_problem(exc: ValidationError) -> str:
    """The same wording the API uses for a 422: 'field: problem'."""
    errors = exc.errors()
    if not errors:
        return "body: invalid"
    first = errors[0]
    where = ".".join(str(p) for p in first["loc"]) or "body"
    return f"{where}: {first['msg']}"


# Account tree


@dataclass
class AccountRow:
    account: Account
    depth: int
    debit: int = 0
    credit: int = 0
    balance: int = 0
    share: int = 0
    children: list[AccountRow] = field(default_factory=list)

    @property
    def is_header(self) -> bool:
        return self.account.usage == "HEADER"

    @property
    def is_zero(self) -> bool:
        return self.debit == 0 and self.credit == 0


def build_tree(accounts: list[Account], totals: dict[str, tuple[int, int]]) -> list[AccountRow]:
    """Accounts as a tree with totals rolled up to every header, at any depth."""
    rows = {a.code: AccountRow(account=a, depth=0) for a in accounts}
    roots: list[AccountRow] = []
    for account in accounts:
        row = rows[account.code]
        parent = rows.get(account.parent_code) if account.parent_code else None
        if parent is None:
            roots.append(row)
        else:
            parent.children.append(row)

    def roll_up(row: AccountRow, depth: int) -> None:
        row.depth = depth
        row.debit, row.credit = totals.get(row.account.code, (0, 0))
        for child in row.children:
            roll_up(child, depth + 1)
            row.debit += child.debit
            row.credit += child.credit
        row.balance = account_service.balance_for(
            row.account.type, debit_total=row.debit, credit_total=row.credit
        )
        spread = sum(abs(c.balance) for c in row.children)
        for child in row.children:
            child.share = round(abs(child.balance) * 100 / spread) if spread else 0

    for root in roots:
        roll_up(root, 0)
    return roots


def flatten(rows: list[AccountRow]) -> list[AccountRow]:
    out: list[AccountRow] = []
    for row in rows:
        out.append(row)
        out.extend(flatten(row.children))
    return out


def accounts_context() -> dict[str, Any]:
    engine = get_engine()
    accounts = account_service.list_accounts(engine)
    tree = build_tree(accounts, queries.account_totals(engine))
    stats = queries.journal_stats(engine)
    by_code = {row.account.code: row for row in flatten(tree)}
    return {
        "tree": tree,
        "stats": stats,
        "account_count": len(accounts),
        "header_count": sum(1 for a in accounts if a.usage == "HEADER"),
        "loan_book": by_code.get("1200"),
        "headers": [a for a in accounts if a.usage == "HEADER"],
    }


def layout_context(active: str) -> dict[str, Any]:
    """What base.html needs on every page: nav state, fault pill, activity rail."""
    engine = get_engine()
    return {
        "active": active,
        "fault": faults.get_setting(engine),
        "fault_labels": FAULT_LABELS,
        "recent": queries.recent_entries(engine, ACTIVITY_SIZE),
        "entry_count": queries.journal_stats(engine).entries,
    }


# Pages


@bp.get("/accounts")
def accounts_page() -> str:
    return render_template(
        "accounts.html", **layout_context("accounts"), **accounts_context(), form=None, error=None
    )


@bp.get("/accounts/table")
def accounts_table() -> str:
    return render_template("partials/accounts_live.html", **accounts_context())


@bp.post("/accounts")
def add_account() -> Response:
    form = {name: request.form.get(name, "").strip() for name in ACCOUNT_FIELDS}
    payload: dict[str, Any] = {k: v for k, v in form.items() if v != ""}
    context = accounts_context()
    try:
        created = account_service.create_account(
            get_engine(), CreateAccountRequest.model_validate(payload)
        )
    except ValidationError as exc:
        body = render_template(
            "partials/account_form.html", form=form, error=first_problem(exc), **context
        )
        return html(body, 422)
    except ErpError as err:
        body = render_template(
            "partials/account_form.html", form=form, error=err.message, **context
        )
        return html(body, err.status if 400 <= err.status < 500 else 422)

    context = accounts_context()
    body = render_template("partials/account_form.html", form=None, error=None, **context)
    body += render_template("partials/accounts_live.html", oob=True, **context)
    if created.status == "CREATED":
        message, kind = f"Account {created.code} {created.name} created", "good"
    else:
        message, kind = f"Account {created.code} already exists with the same details", "info"
    return with_toast(html(body), message, kind)


@bp.get("/activity")
def activity() -> str:
    return render_template("partials/activity.html", **layout_context(""), oob=True)


@bp.get("/faults")
def faults_page() -> str:
    return render_template("faults.html", **layout_context("faults"), tiles=FAULT_TILES, error=None)


@bp.get("/faults/state")
def faults_state() -> str:
    return render_template(
        "partials/fault_state.html",
        fault=faults.get_setting(get_engine()),
        tiles=FAULT_TILES,
        fault_labels=FAULT_LABELS,
    )


def fault_response(setting: FaultSettingOut, message: str, kind: str) -> Response:
    body = render_template(
        "partials/fault_state.html",
        fault=setting,
        tiles=FAULT_TILES,
        fault_labels=FAULT_LABELS,
        clear_error=True,
    )
    body += render_template(
        "partials/status.html", fault=setting, fault_labels=FAULT_LABELS, oob=True
    )
    return with_toast(html(body), message, kind)


@bp.post("/faults")
def set_fault() -> Response:
    form = {name: request.form.get(name, "").strip() for name in FAULT_FIELDS}
    payload: dict[str, Any] = {k: v for k, v in form.items() if v != ""}
    try:
        wanted = FaultSettingIn.model_validate(payload)
    except ValidationError as exc:
        response = html(render_template("partials/fault_error.html", error=first_problem(exc)), 422)
        response.headers["HX-Retarget"] = "#fault-error"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    delay = wanted.delay_seconds or faults.default_delay(current_app.config["ERP_TIMEOUT_DELAY"])
    setting = faults.set_fault(
        get_engine(),
        wanted.mode,
        path_prefix=wanted.path_prefix,
        delay_seconds=delay,
        remaining=wanted.remaining,
        retry_after=wanted.retry_after,
    )
    scope = "until cleared" if setting.remaining is None else f"next {setting.remaining} calls"
    if setting.remaining == 1:
        scope = "next call only"
    message = f"{FAULT_LABELS[wanted.mode]} switched on · {setting.path_prefix} · {scope}"
    return fault_response(setting, message, "warn")


@bp.post("/faults/clear")
def clear_fault() -> Response:
    setting = faults.clear_fault(get_engine())
    return fault_response(setting, "Fault cleared · the ERP answers normally", "good")
