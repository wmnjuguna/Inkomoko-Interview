"""Read-only numbers for the UI: journal counts, totals per account, latest entries.

Plain SQLAlchemy Core on the tables, so the pages do not depend on the journal service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Engine, func, select

from mock_erp.models import journal_entries, journal_lines


@dataclass(frozen=True)
class JournalStats:
    entries: int
    debit_total: int
    credit_total: int


@dataclass(frozen=True)
class RecentEntry:
    external_reference: str
    source: str
    entry_date: date
    description: str | None
    reverses: str | None
    amount: int
    created_at: datetime


def journal_stats(engine: Engine) -> JournalStats:
    """How many entries, and the sum of every debit and every credit line."""
    with engine.connect() as conn:
        entries = conn.execute(select(func.count()).select_from(journal_entries)).scalar_one()
        debit, credit = conn.execute(
            select(
                func.coalesce(func.sum(journal_lines.c.debit), 0),
                func.coalesce(func.sum(journal_lines.c.credit), 0),
            )
        ).one()
    return JournalStats(entries=int(entries), debit_total=int(debit), credit_total=int(credit))


def account_totals(engine: Engine) -> dict[str, tuple[int, int]]:
    """Debit and credit totals per account that has lines: {code: (debit, credit)}."""
    statement = select(
        journal_lines.c.account_code,
        func.sum(journal_lines.c.debit),
        func.sum(journal_lines.c.credit),
    ).group_by(journal_lines.c.account_code)
    with engine.connect() as conn:
        return {code: (int(dr), int(cr)) for code, dr, cr in conn.execute(statement)}


def recent_entries(engine: Engine, limit: int = 8) -> list[RecentEntry]:
    """The newest entries first, each with its amount (the sum of its debits)."""
    amount = (
        select(func.coalesce(func.sum(journal_lines.c.debit), 0))
        .where(journal_lines.c.entry_id == journal_entries.c.id)
        .scalar_subquery()
    )
    statement = (
        select(
            journal_entries.c.external_reference,
            journal_entries.c.source,
            journal_entries.c.entry_date,
            journal_entries.c.description,
            journal_entries.c.reverses,
            amount.label("amount"),
            journal_entries.c.created_at,
        )
        .order_by(journal_entries.c.created_at.desc(), journal_entries.c.external_reference.desc())
        .limit(limit)
    )
    with engine.connect() as conn:
        rows = conn.execute(statement).mappings().all()
    return [RecentEntry(**{**row, "amount": int(row["amount"])}) for row in rows]
