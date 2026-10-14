"""Smoke tests for the journal pages under /ui: list, detail and the manual entry form."""

from __future__ import annotations

import re
from datetime import date

from flask.testing import FlaskClient
from sqlalchemy import Engine

from mock_erp.schemas.journal import EntrySource, JournalEntryIn, JournalLineIn
from mock_erp.services import journal as journal_service

HX = {"HX-Request": "true"}


def post(
    engine: Engine,
    ref: str,
    lines: list[tuple[str, int, int]],
    *,
    source: EntrySource = "DISBURSEMENT",
    entry_date: date = date(2026, 10, 12),
    description: str | None = None,
    reverses: str | None = None,
) -> str:
    entry = JournalEntryIn(
        external_reference=ref,
        entry_date=entry_date,
        source=source,
        description=description,
        reverses=reverses,
        lines=[JournalLineIn(account_code=c, debit=d, credit=cr) for c, d, cr in lines],
    )
    return str(journal_service.post_entry(engine, entry, "c-test").erp_transaction_id)


def seed(engine: Engine) -> str:
    """A disbursement and a repayment. Returns the disbursement's ERP id."""
    erp_id = post(
        engine,
        "DISB-UI-1",
        [("1200", 250_000, 0), ("1100", 0, 250_000)],
        description="Loan for Testperson",
    )
    post(
        engine,
        "REPAY-UI-1",
        [("1100", 31_000, 0), ("1200", 0, 25_000), ("4100", 0, 6_000)],
        source="REPAYMENT",
        entry_date=date(2026, 10, 13),
        description="First instalment",
    )
    return erp_id


def entry_form(**overrides: object) -> dict[str, object]:
    form: dict[str, object] = {
        "external_reference": "MAN-UI-1",
        "entry_date": "2026-10-13",
        "source": "MANUAL",
        "description": "Opening cash",
        "reverses": "",
        "account_code": ["1100", "2100"],
        "debit": ["500", ""],
        "credit": ["", "500"],
    }
    form.update(overrides)
    return form


def count_of(page: str, source: str) -> str:
    found = re.search(rf'id="count-{source}"[^>]*>(\d+)<', page)
    assert found, f"no count chip for {source}"
    return found.group(1)


# Journal list


def test_journal_page_lists_entries(client: FlaskClient, db: Engine) -> None:
    seed(db)
    response = client.get("/ui/journal")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "<html" in page
    assert "DISB-UI-1" in page
    assert "REPAY-UI-1" in page
    assert "Loan for Testperson" in page
    assert 'href="/ui/journal" aria-current="page"' in page
    assert count_of(page, "ALL") == "2"
    assert count_of(page, "REPAYMENT") == "1"
    assert count_of(page, "REVERSAL") == "0"


def test_journal_page_with_no_entries_has_an_empty_state(client: FlaskClient) -> None:
    page = client.get("/ui/journal").get_data(as_text=True)
    assert "No entries yet" in page
    assert 'href="/ui/journal/new"' in page


def test_search_narrows_by_reference_and_description(client: FlaskClient, db: Engine) -> None:
    seed(db)
    by_ref = client.get("/ui/journal/_list?q=repay", headers=HX).get_data(as_text=True)
    assert "REPAY-UI-1" in by_ref
    assert "DISB-UI-1" not in by_ref
    assert "<html" not in by_ref

    by_text = client.get("/ui/journal/_list?q=Testperson", headers=HX).get_data(as_text=True)
    assert "DISB-UI-1" in by_text
    assert "REPAY-UI-1" not in by_text

    nothing = client.get("/ui/journal/_list?q=zzz", headers=HX).get_data(as_text=True)
    assert "No entries match" in nothing


def test_source_filter_narrows_and_counts_follow_the_search(
    client: FlaskClient, db: Engine
) -> None:
    seed(db)
    fragment = client.get("/ui/journal/_list?source=DISBURSEMENT", headers=HX).get_data(
        as_text=True
    )
    assert "DISB-UI-1" in fragment
    assert "REPAY-UI-1" not in fragment
    assert count_of(fragment, "ALL") == "2"

    searched = client.get("/ui/journal/_list?q=DISB", headers=HX).get_data(as_text=True)
    assert count_of(searched, "ALL") == "1"
    assert count_of(searched, "REPAYMENT") == "0"


def test_date_range_filter(client: FlaskClient, db: Engine) -> None:
    seed(db)
    fragment = client.get(
        "/ui/journal/_list?date_from=2026-10-13&date_to=2026-10-31", headers=HX
    ).get_data(as_text=True)
    assert "REPAY-UI-1" in fragment
    assert "DISB-UI-1" not in fragment


def test_list_refresh_keeps_the_selected_entry(client: FlaskClient, db: Engine) -> None:
    seed(db)
    fragment = client.get("/ui/journal/_list?selected=DISB-UI-1", headers=HX).get_data(as_text=True)
    card = re.search(r'<a[^>]*data-ref="DISB-UI-1"[^>]*>', fragment)
    assert card and 'aria-current="true"' in card.group(0)
    other = re.search(r'<a[^>]*data-ref="REPAY-UI-1"[^>]*>', fragment)
    assert other and 'aria-current="true"' not in other.group(0)


# Entry detail


def test_entry_detail_full_page(client: FlaskClient, db: Engine) -> None:
    erp_id = seed(db)
    response = client.get("/ui/journal/DISB-UI-1")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "<html" in page
    detail = page[page.index('id="entry-detail"') :]
    assert "Loan Receivable" in detail
    assert "Bank" in detail
    assert "1200" in detail
    assert "Balanced" in detail
    assert erp_id in detail
    assert "c-test" in detail
    # Both sides show their total.
    assert detail.count("250,000") >= 2


def test_entry_detail_partial_and_reversal_link(client: FlaskClient, db: Engine) -> None:
    seed(db)
    post(
        db,
        "REV-UI-1",
        [("1100", 250_000, 0), ("1200", 0, 250_000)],
        source="REVERSAL",
        reverses="DISB-UI-1",
    )
    response = client.get("/ui/journal/REV-UI-1", headers=HX)
    assert response.status_code == 200
    fragment = response.get_data(as_text=True)
    assert "<html" not in fragment
    assert "Reverses" in fragment
    assert 'href="/ui/journal/DISB-UI-1"' in fragment


def test_unknown_entry_is_a_404_page(client: FlaskClient) -> None:
    response = client.get("/ui/journal/NOPE-1")
    assert response.status_code == 404
    page = response.get_data(as_text=True)
    assert "<html" in page
    assert "No journal entry NOPE-1." in page


def test_activity_rail_links_to_entries(client: FlaskClient, db: Engine) -> None:
    seed(db)
    fragment = client.get("/ui/activity", headers=HX).get_data(as_text=True)
    assert 'href="/ui/journal/DISB-UI-1"' in fragment


# New entry


def test_new_entry_page_offers_detail_accounts_only(client: FlaskClient) -> None:
    response = client.get("/ui/journal/new")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'href="/ui/journal/new" aria-current="page"' in page
    assert "Balance check" in page
    assert 'value="1100"' in page
    assert 'value="1000"' not in page
    assert f'value="{date.today().isoformat()}"' in page


def test_posting_a_balanced_entry_creates_it(client: FlaskClient, db: Engine) -> None:
    response = client.post("/ui/journal/new", data=entry_form(), headers=HX)
    assert response.status_code == 201
    fragment = response.get_data(as_text=True)
    assert 'href="/ui/journal/MAN-UI-1"' in fragment
    assert "MAN-UI-1" in response.headers["HX-Trigger"]

    stored = journal_service.get_entry(db, "MAN-UI-1")
    assert stored.source == "MANUAL"
    assert stored.total == 500
    assert stored.description == "Opening cash"
    assert stored.correlation_id


def test_posting_the_same_entry_again_changes_nothing(client: FlaskClient, db: Engine) -> None:
    client.post("/ui/journal/new", data=entry_form(), headers=HX)
    again = client.post("/ui/journal/new", data=entry_form(), headers=HX)
    assert again.status_code == 200
    assert "Already posted — nothing changed" in again.get_data(as_text=True)
    assert len(journal_service.search_entries(db, references=["MAN-UI-1"])) == 1


def test_same_reference_with_other_amounts_is_a_conflict(client: FlaskClient) -> None:
    client.post("/ui/journal/new", data=entry_form(), headers=HX)
    changed = entry_form(debit=["700", ""], credit=["", "700"])
    response = client.post("/ui/journal/new", data=changed, headers=HX)
    assert response.status_code == 409
    fragment = response.get_data(as_text=True)
    assert "MAN-UI-1 is already stored with different content." in fragment
    assert 'value="700"' in fragment


def test_unbalanced_lines_show_the_api_message(client: FlaskClient, db: Engine) -> None:
    response = client.post("/ui/journal/new", data=entry_form(credit=["", "400"]), headers=HX)
    assert response.status_code == 422
    fragment = response.get_data(as_text=True)
    assert "Debits are 500 and credits are 400." in fragment
    # The form keeps what was typed.
    assert 'value="MAN-UI-1"' in fragment
    assert 'value="400"' in fragment
    assert 'value="Opening cash"' in fragment
    assert journal_service.search_entries(db, references=["MAN-UI-1"]) == []


def test_a_header_account_line_is_refused(client: FlaskClient) -> None:
    response = client.post(
        "/ui/journal/new", data=entry_form(account_code=["1000", "2100"]), headers=HX
    )
    assert response.status_code == 422
    assert "HEADER accounts take no lines: 1000." in response.get_data(as_text=True)


def test_a_reversal_needs_the_entry_it_reverses(client: FlaskClient) -> None:
    response = client.post("/ui/journal/new", data=entry_form(source="REVERSAL"), headers=HX)
    assert response.status_code == 422
    assert "a REVERSAL must set reverses" in response.get_data(as_text=True)


def test_a_reversal_of_an_unknown_entry_is_refused(client: FlaskClient) -> None:
    response = client.post(
        "/ui/journal/new", data=entry_form(source="REVERSAL", reverses="GHOST-1"), headers=HX
    )
    assert response.status_code == 422
    assert "reverses: no entry GHOST-1." in response.get_data(as_text=True)


def test_blank_line_rows_are_ignored(client: FlaskClient, db: Engine) -> None:
    form = entry_form(
        account_code=["1100", "", "2100"], debit=["500", "", ""], credit=["", "", "500"]
    )
    response = client.post("/ui/journal/new", data=form, headers=HX)
    assert response.status_code == 201
    assert len(journal_service.get_entry(db, "MAN-UI-1").lines) == 2


def test_post_without_the_htmx_header_is_refused(client: FlaskClient, db: Engine) -> None:
    response = client.post("/ui/journal/new", data=entry_form())
    assert response.status_code == 400
    assert journal_service.search_entries(db, references=["MAN-UI-1"]) == []
