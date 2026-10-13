"""The deferred balance trigger: the last guard for writes made around Python (raw SQL)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from mock_erp.errors import ErrorCode
from mock_erp.services.journal import map_trigger_error


def insert_raw(engine: Engine, lines: list[tuple[str, int, int]]) -> None:
    """Write one entry and its lines with plain SQL, in one transaction."""
    entry_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO journal_entries (id, external_reference, request_hash, entry_date, "
                "source) VALUES (:id, :ref, 'raw', DATE '2026-10-12', 'MANUAL')"
            ),
            {"id": entry_id, "ref": f"RAW-{entry_id.hex[:8]}"},
        )
        for code, debit, credit in lines:
            conn.execute(
                text(
                    "INSERT INTO journal_lines (entry_id, account_code, debit, credit) "
                    "VALUES (:id, :code, :debit, :credit)"
                ),
                {"id": entry_id, "code": code, "debit": debit, "credit": credit},
            )


def count_entries(engine: Engine) -> Any:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM journal_entries")).scalar_one()


def test_balanced_raw_insert_is_kept(db: Engine) -> None:
    insert_raw(db, [("1100", 500, 0), ("1200", 0, 500)])
    assert count_entries(db) == 1


def test_unbalanced_raw_insert_fails_at_commit(db: Engine) -> None:
    with pytest.raises(IntegrityError) as caught:
        insert_raw(db, [("1100", 500, 0), ("1200", 0, 400)])
    assert count_entries(db) == 0
    err = map_trigger_error(caught.value)
    assert err is not None
    assert err.code == ErrorCode.ENTRY_NOT_BALANCED
    assert err.status == 422


def test_line_on_a_header_account_fails_at_commit(db: Engine) -> None:
    with pytest.raises(IntegrityError) as caught:
        insert_raw(db, [("1000", 500, 0), ("1200", 0, 500)])
    assert count_entries(db) == 0
    err = map_trigger_error(caught.value)
    assert err is not None
    assert err.code == ErrorCode.HEADER_ACCOUNT
    assert err.status == 422
