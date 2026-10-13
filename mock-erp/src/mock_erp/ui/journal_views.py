"""Journal pages under /ui: the entry list, one entry's detail, and the manual entry form.

Registered on the same `ui` blueprint; views.py imports this module at its end.
Every read and write goes through mock_erp.services.journal.

Note: /ui/journal/new and /ui/journal/_list are fixed routes, so a reference literally
named "new" opens the form instead of the entry ("_list" cannot be a reference).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, get_args

from flask import Response, g, render_template, request
from pydantic import ValidationError

from mock_erp.db import get_engine
from mock_erp.errors import ErpError, ErrorCode
from mock_erp.schemas.accounts import Account
from mock_erp.schemas.journal import EntrySource, JournalEntryIn, JournalEntryOut
from mock_erp.services import accounts as account_service
from mock_erp.services import journal as journal_service
from mock_erp.ui.views import bp, first_problem, html, layout_context, with_toast

SOURCES: tuple[str, ...] = get_args(EntrySource)
SEARCH_LIMIT = 1000
DEFAULT_LINES = 2


# Filters


@dataclass(frozen=True)
class JournalFilters:
    q: str = ""
    source: str = ""
    date_from: date | None = None
    date_to: date | None = None
    selected: str = ""

    def query(self, **changes: str) -> dict[str, str]:
        """The filters as query parameters, for links that keep them."""
        values = {
            "q": self.q,
            "source": self.source,
            "date_from": self.date_from.isoformat() if self.date_from else "",
            "date_to": self.date_to.isoformat() if self.date_to else "",
        }
        values.update(changes)
        return {k: v for k, v in values.items() if v}

    @property
    def active(self) -> bool:
        return bool(self.q or self.source or self.date_from or self.date_to)


def _date_arg(name: str) -> date | None:
    raw = request.args.get(name, "").strip()
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def read_filters(selected: str = "") -> JournalFilters:
    source = request.args.get("source", "").strip().upper()
    return JournalFilters(
        q=request.args.get("q", "").strip()[:200],
        source=source if source in SOURCES else "",
        date_from=_date_arg("date_from"),
        date_to=_date_arg("date_to"),
        selected=selected or request.args.get("selected", "").strip(),
    )


def list_context(filters: JournalFilters) -> dict[str, Any]:
    """Entries matching the search and dates; counts per source; the shown slice."""
    found = journal_service.search_entries(
        get_engine(),
        date_from=filters.date_from,
        date_to=filters.date_to,
        q=filters.q or None,
        limit=SEARCH_LIMIT,
    )
    counts = {"ALL": len(found), **{s: 0 for s in SOURCES}}
    for entry in found:
        counts[entry.source] += 1
    shown = [e for e in found if not filters.source or e.source == filters.source]
    return {
        "filters": filters,
        "entries": shown,
        "counts": counts,
        "capped": len(found) >= SEARCH_LIMIT,
        "sources": SOURCES,
    }


def pick_selected(filters: JournalFilters, entries: list[JournalEntryOut]) -> JournalFilters:
    """With nothing chosen, the newest shown entry is the selected one."""
    if filters.selected or not entries:
        return filters
    return JournalFilters(
        q=filters.q,
        source=filters.source,
        date_from=filters.date_from,
        date_to=filters.date_to,
        selected=entries[0].external_reference,
    )


def is_partial() -> bool:
    """An HTMX swap, but not a history restore (which needs the whole page)."""
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )


def journal_page(filters: JournalFilters, detail: JournalEntryOut | None) -> str:
    """The whole journal page. The tab title names the entry only on its own URL."""
    title_ref = detail.external_reference if detail else ""
    context = list_context(filters)
    context["filters"] = pick_selected(filters, context["entries"])
    if detail is None and context["entries"]:
        detail = context["entries"][0]
    return render_template(
        "journal.html",
        **layout_context("journal"),
        **context,
        entry=detail,
        title_ref=title_ref,
    )


@bp.get("/journal")
def journal_list_page() -> str:
    return journal_page(read_filters(), None)


@bp.get("/journal/_list")
def journal_list_partial() -> str:
    context = list_context(read_filters())
    context["filters"] = pick_selected(context["filters"], context["entries"])
    return render_template("partials/journal_list.html", **context)


@bp.get("/journal/<path:ref>")
def journal_entry(ref: str) -> str | Response:
    try:
        entry = journal_service.get_entry(get_engine(), ref)
    except ErpError as err:
        if err.code != ErrorCode.NOT_FOUND:
            raise
        if is_partial():
            return html(render_template("partials/entry_missing.html", message=err.message), 404)
        body = render_template(
            "not_found.html", **layout_context("journal"), message=err.message, ref=ref
        )
        return html(body, 404)
    if is_partial():
        return render_template("partials/entry_detail.html", entry=entry, partial=True)
    return journal_page(read_filters(selected=ref), entry)


# New entry


@dataclass
class LineRow:
    account_code: str = ""
    debit: str = ""
    credit: str = ""

    @property
    def blank(self) -> bool:
        return not (self.account_code or self.debit or self.credit)


@dataclass
class EntryForm:
    external_reference: str = ""
    entry_date: str = ""
    source: str = "MANUAL"
    description: str = ""
    reverses: str = ""
    lines: list[LineRow] = field(default_factory=list)

    @property
    def debit_total(self) -> int:
        return sum(_amount_or_zero(line.debit) for line in self.lines)

    @property
    def credit_total(self) -> int:
        return sum(_amount_or_zero(line.credit) for line in self.lines)

    @property
    def can_post(self) -> bool:
        used = [line for line in self.lines if not line.blank]
        return len(used) >= 2 and self.debit_total == self.credit_total > 0


def _amount(raw: str) -> int | str:
    """Whole RWF from what was typed; spaces and thousands commas are fine.
    Text that is not a number is passed on, so validation names the field."""
    cleaned = raw.replace(",", "").replace(" ", "").replace("\u00a0", "")
    if cleaned == "":
        return 0
    return int(cleaned) if cleaned.isdigit() else raw


def _amount_or_zero(raw: str) -> int:
    value = _amount(raw)
    return value if isinstance(value, int) else 0


def blank_form() -> EntryForm:
    return EntryForm(
        entry_date=date.today().isoformat(), lines=[LineRow() for _ in range(DEFAULT_LINES)]
    )


def read_entry_form() -> EntryForm:
    codes = request.form.getlist("account_code")
    debits = request.form.getlist("debit")
    credits = request.form.getlist("credit")
    size = max(len(codes), len(debits), len(credits))

    def at(values: list[str], i: int) -> str:
        return values[i].strip() if i < len(values) else ""

    lines = [LineRow(at(codes, i), at(debits, i), at(credits, i)) for i in range(size)]
    return EntryForm(
        external_reference=request.form.get("external_reference", "").strip(),
        entry_date=request.form.get("entry_date", "").strip(),
        source=request.form.get("source", "MANUAL").strip() or "MANUAL",
        description=request.form.get("description", "").strip(),
        reverses=request.form.get("reverses", "").strip(),
        lines=lines or [LineRow() for _ in range(DEFAULT_LINES)],
    )


def entry_payload(form: EntryForm) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "external_reference": form.external_reference,
        "entry_date": form.entry_date,
        "source": form.source,
        "lines": [
            {
                "account_code": line.account_code,
                "debit": _amount(line.debit),
                "credit": _amount(line.credit),
            }
            for line in form.lines
            if not line.blank
        ],
    }
    if form.description:
        payload["description"] = form.description
    if form.reverses:
        payload["reverses"] = form.reverses
    return payload


def detail_accounts() -> list[Account]:
    return account_service.list_accounts(get_engine(), "DETAIL")


def render_entry_form(form: EntryForm, **notice: Any) -> str:
    return render_template(
        "partials/entry_form.html",
        form=form,
        accounts=detail_accounts(),
        sources=SOURCES,
        notice=notice or None,
    )


@bp.get("/journal/new")
def new_entry_page() -> str:
    return render_template(
        "new_entry.html",
        **layout_context("new"),
        form=blank_form(),
        accounts=detail_accounts(),
        sources=SOURCES,
        notice=None,
    )


@bp.post("/journal/new")
def post_entry() -> Response:
    form = read_entry_form()
    try:
        entry = JournalEntryIn.model_validate(entry_payload(form))
        posted = journal_service.post_entry(get_engine(), entry, g.get("correlation_id"))
    except ValidationError as exc:
        return html(render_entry_form(form, kind="bad", message=first_problem(exc)), 422)
    except ErpError as err:
        status = err.status if 400 <= err.status < 500 else 422
        return html(render_entry_form(form, kind="bad", message=err.message), status)

    ref = posted.external_reference
    if posted.status == "ALREADY_EXISTS":
        body = render_entry_form(
            form,
            kind="info",
            message="Already posted — nothing changed",
            detail=f"{ref} is stored with exactly these lines, so this was a safe repeat.",
            ref=ref,
        )
        return with_toast(html(body), f"{ref} was already posted", "info")
    body = render_entry_form(
        blank_form(),
        kind="good",
        message=f"Posted {ref}",
        detail=f"{posted.total:,} RWF on each side · {len(posted.lines)} lines.",
        ref=ref,
    )
    return with_toast(html(body, 201), f"Entry {ref} posted", "good")
