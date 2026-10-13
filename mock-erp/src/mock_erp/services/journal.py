"""Journal entries: the rules, the idempotent write, and the reads. No Flask here."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import Connection, Engine, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from mock_erp.errors import ErpError, ErrorCode
from mock_erp.hashing import request_hash
from mock_erp.models import accounts, journal_entries, journal_lines
from mock_erp.schemas.journal import (
    EntrySource,
    JournalEntryIn,
    JournalEntryOut,
    JournalLineOut,
    JournalPostOut,
)


def entry_hash(entry: JournalEntryIn) -> str:
    """Hash of the business content. Key order and line order do not matter."""
    lines = sorted((line.account_code, line.debit, line.credit) for line in entry.lines)
    return request_hash(
        {
            "external_reference": entry.external_reference,
            "entry_date": entry.entry_date.isoformat(),
            "source": entry.source,
            "description": entry.description,
            "reverses": entry.reverses,
            "lines": [{"account_code": a, "debit": d, "credit": c} for a, d, c in lines],
        }
    )


def check_balanced(entry: JournalEntryIn) -> None:
    """Debits must equal credits."""
    debits = sum(line.debit for line in entry.lines)
    credits = sum(line.credit for line in entry.lines)
    if debits != credits:
        raise ErpError(
            ErrorCode.ENTRY_NOT_BALANCED,
            f"Debits are {debits} and credits are {credits}.",
            422,
            {"debits": debits, "credits": credits},
        )


TRIGGER_ERRORS = {
    "journal_entries_balanced": (ErrorCode.ENTRY_NOT_BALANCED, "Debits and credits differ."),
    "journal_lines_detail_only": (ErrorCode.HEADER_ACCOUNT, "A line posts to a HEADER account."),
}


def map_trigger_error(exc: IntegrityError) -> ErpError | None:
    """Turn a failure of the balance trigger into a 422. None if it was something else."""
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name not in TRIGGER_ERRORS:
        return None
    code, message = TRIGGER_ERRORS[name]
    return ErpError(code, message, 422)


def post_entry(engine: Engine, entry: JournalEntryIn, correlation_id: str | None) -> JournalPostOut:
    """Write an entry once. A retry with the same content gets the stored entry back.

    Order of checks: the shape (Pydantic), the balance and the accounts come first, so a
    malformed retry still fails with 422 even when its reference is already stored. Only a
    valid entry reaches the insert-or-return step, where the same reference with the same
    hash gives ALREADY_EXISTS and with a different hash gives 409 reference_conflict.
    The insert uses ON CONFLICT DO NOTHING, so racing posts of one reference make one row.
    """
    check_balanced(entry)
    digest = entry_hash(entry)
    try:
        with engine.begin() as conn:
            _check_accounts(conn, entry)
            _check_reverses(conn, entry)
            new_id = conn.execute(
                pg_insert(journal_entries)
                .values(
                    id=uuid.uuid4(),
                    external_reference=entry.external_reference,
                    request_hash=digest,
                    entry_date=entry.entry_date,
                    source=entry.source,
                    reverses=entry.reverses,
                    description=entry.description,
                    correlation_id=correlation_id,
                )
                .on_conflict_do_nothing(index_elements=["external_reference"])
                .returning(journal_entries.c.id)
            ).scalar_one_or_none()
            if new_id is None:
                return _existing(conn, entry.external_reference, digest)
            conn.execute(
                journal_lines.insert(),
                [
                    {
                        "entry_id": new_id,
                        "account_code": line.account_code,
                        "debit": line.debit,
                        "credit": line.credit,
                    }
                    for line in entry.lines
                ],
            )
            stored = _load(conn, [journal_entries.c.id == new_id], limit=1)[0]
    except IntegrityError as exc:
        mapped = map_trigger_error(exc)
        if mapped is None:
            raise
        raise mapped from exc
    return JournalPostOut(**stored.model_dump(), status="CREATED")


def get_entry(engine: Engine, external_reference: str) -> JournalEntryOut:
    """One entry by its reference, or 404 not_found."""
    with engine.connect() as conn:
        found = _load(conn, [journal_entries.c.external_reference == external_reference], limit=1)
    if not found:
        raise ErpError(
            ErrorCode.NOT_FOUND,
            f"No journal entry {external_reference}.",
            404,
            {"external_reference": external_reference},
        )
    return found[0]


def search_entries(
    engine: Engine,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    references: Sequence[str] | None = None,
    source: EntrySource | None = None,
    q: str | None = None,
    limit: int = 500,
) -> list[JournalEntryOut]:
    """Entries matching every filter given, newest first (entry_date, then created_at)."""
    where: list[Any] = []
    if date_from is not None:
        where.append(journal_entries.c.entry_date >= date_from)
    if date_to is not None:
        where.append(journal_entries.c.entry_date <= date_to)
    if references:
        where.append(journal_entries.c.external_reference.in_(list(references)))
    if source is not None:
        where.append(journal_entries.c.source == source)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where.append(
            or_(
                journal_entries.c.external_reference.ilike(pattern, escape="\\"),
                journal_entries.c.description.ilike(pattern, escape="\\"),
            )
        )
    with engine.connect() as conn:
        return _load(conn, where, limit=limit)


def _existing(conn: Connection, external_reference: str, digest: str) -> JournalPostOut:
    stored_hash = conn.execute(
        select(journal_entries.c.request_hash).where(
            journal_entries.c.external_reference == external_reference
        )
    ).scalar_one()
    if stored_hash != digest:
        raise ErpError(
            ErrorCode.REFERENCE_CONFLICT,
            f"{external_reference} is already stored with different content.",
            409,
            {"external_reference": external_reference},
        )
    stored = _load(conn, [journal_entries.c.external_reference == external_reference], limit=1)
    return JournalPostOut(**stored[0].model_dump(), status="ALREADY_EXISTS")


def _check_accounts(conn: Connection, entry: JournalEntryIn) -> None:
    """Every line needs a known DETAIL account."""
    codes = {line.account_code for line in entry.lines}
    rows = conn.execute(
        select(accounts.c.code, accounts.c.usage).where(accounts.c.code.in_(codes))
    ).all()
    usage: dict[str, str] = {row.code: row.usage for row in rows}
    unknown = sorted(codes - usage.keys())
    if unknown:
        raise ErpError(
            ErrorCode.UNKNOWN_ACCOUNT,
            f"Unknown account codes: {', '.join(unknown)}.",
            422,
            {"account_codes": unknown},
        )
    headers = sorted(code for code, kind in usage.items() if kind == "HEADER")
    if headers:
        raise ErpError(
            ErrorCode.HEADER_ACCOUNT,
            f"HEADER accounts take no lines: {', '.join(headers)}.",
            422,
            {"account_codes": headers},
        )


def _check_reverses(conn: Connection, entry: JournalEntryIn) -> None:
    """A reversal must point at an entry that is already stored."""
    if entry.reverses is None:
        return
    found = conn.execute(
        select(journal_entries.c.id).where(journal_entries.c.external_reference == entry.reverses)
    ).first()
    if found is None:
        raise ErpError(
            ErrorCode.INVALID_REQUEST,
            f"reverses: no entry {entry.reverses}.",
            422,
            {"reverses": entry.reverses},
        )


def _load(conn: Connection, where: Sequence[Any], limit: int) -> list[JournalEntryOut]:
    """Entries matching the filters, newest first, each with its lines in posting order."""
    heads = conn.execute(
        select(journal_entries)
        .where(*where)
        .order_by(
            journal_entries.c.entry_date.desc(),
            journal_entries.c.created_at.desc(),
            journal_entries.c.external_reference,
        )
        .limit(limit)
    ).all()
    if not heads:
        return []
    line_rows = conn.execute(
        select(
            journal_lines.c.entry_id,
            journal_lines.c.account_code,
            accounts.c.name,
            journal_lines.c.debit,
            journal_lines.c.credit,
        )
        .join(accounts, accounts.c.code == journal_lines.c.account_code)
        .where(journal_lines.c.entry_id.in_([h.id for h in heads]))
        .order_by(journal_lines.c.id)
    ).all()
    lines: dict[uuid.UUID, list[JournalLineOut]] = {h.id: [] for h in heads}
    for row in line_rows:
        lines[row.entry_id].append(
            JournalLineOut(
                account_code=row.account_code,
                account_name=row.name,
                debit=row.debit,
                credit=row.credit,
            )
        )
    return [
        JournalEntryOut(
            erp_transaction_id=h.id,
            external_reference=h.external_reference,
            entry_date=h.entry_date,
            source=h.source,
            description=h.description,
            reverses=h.reverses,
            total=sum(line.debit for line in lines[h.id]),
            lines=lines[h.id],
            created_at=h.created_at,
            correlation_id=h.correlation_id,
        )
        for h in heads
    ]
