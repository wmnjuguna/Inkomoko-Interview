"""GL sync rules with fake ERP and Fineract clients.

The clients are simple fakes, so every Fineract answer can be chosen. The sync state and
the run lock live in Postgres, so these tests use the test database (marked integration)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select, text

from integration_service.clients.erp import ErpAccount
from integration_service.clients.errors import (
    FineractConflict,
    FineractRejected,
    FineractServerError,
    FineractTimeout,
    FineractUnavailable,
)
from integration_service.clients.fineract import ErpAccountMapping, ErpAccountSyncResult
from integration_service.models import gl_sync
from integration_service.services.gl_sync import (
    CHANGED_IN_ERP,
    GL_SYNC_LOCK_KEY,
    SyncInProgressError,
    get_report,
    idempotency_key,
    sync_all,
    sync_one,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def erp_account(code: str, erp_type: str, usage: str, parent: str | None, name: str) -> ErpAccount:
    return ErpAccount(
        code=code,
        external_reference=f"GL-{code}",
        name=name,
        type=erp_type,
        usage=usage,
        parent_code=parent,
        currency="RWF",
        created_at=NOW,
    )


def chart() -> list[ErpAccount]:
    return [
        erp_account("1000", "ASSET", "HEADER", None, "Assets"),
        erp_account("1100", "ASSET", "DETAIL", "1000", "Bank"),
        erp_account("2000", "LIABILITY", "HEADER", None, "Liabilities"),
        erp_account("2100", "LIABILITY", "DETAIL", "2000", "Overpayment Liability"),
    ]


def conflict(code: str) -> FineractConflict:
    return FineractConflict(409, {"errors": [{"userMessageGlobalisationCode": code}]})


class FakeErp:
    def __init__(self, accounts: list[ErpAccount]) -> None:
        self.accounts = accounts
        self.calls = 0

    def list_accounts(self, correlation_id: str | None = None) -> list[ErpAccount]:
        self.calls += 1
        return list(self.accounts)


class FakeFineract:
    """Holds synced accounts like the plugin. Failures are chosen per call."""

    def __init__(self) -> None:
        self.synced: dict[str, tuple[int, dict[str, Any]]] = {}
        self.puts: list[tuple[str, dict[str, Any], str | None]] = []
        self.gets: list[str] = []
        self.down = False
        self.lookup_down = False
        # reference -> what the next PUT does: "timeout_after_write", "timeout", or an error.
        self.next_put: dict[str, Any] = {}
        self.taken_codes: set[str] = set()

    def put_erp_account(
        self,
        erp_reference: str,
        body: dict[str, Any],
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> ErpAccountSyncResult:
        self.puts.append((erp_reference, body, idempotency_key))
        if self.down:
            raise FineractUnavailable("connection refused")
        action = self.next_put.pop(erp_reference, None)
        if isinstance(action, Exception):
            raise action
        if action == "timeout":
            raise FineractTimeout("read timeout")
        if body["glCode"] in self.taken_codes:
            raise conflict("error.msg.glaccount.sync.code.conflict")
        parent = body.get("parentAccount")
        if parent is not None and parent not in self.synced:
            raise conflict("error.msg.glaccount.sync.parent.not.synced")
        outcome = "UNCHANGED"
        if erp_reference not in self.synced:
            self.synced[erp_reference] = (len(self.synced) + 100, body)
            outcome = "CREATED"
        if action == "timeout_after_write":
            raise FineractTimeout("read timeout after the write")
        gl_id, stored = self.synced[erp_reference]
        return ErpAccountSyncResult(
            erp_reference=erp_reference,
            gl_account_id=gl_id,
            gl_code=stored["glCode"],
            outcome=outcome,
        )

    def get_erp_account(
        self, erp_reference: str, correlation_id: str | None = None
    ) -> ErpAccountMapping | None:
        self.gets.append(erp_reference)
        if self.down or self.lookup_down:
            raise FineractUnavailable("connection refused")
        if erp_reference not in self.synced:
            return None
        gl_id, body = self.synced[erp_reference]
        return ErpAccountMapping(
            erp_reference=erp_reference,
            gl_account_id=gl_id,
            gl_code=body["glCode"],
            name=body["name"],
            type="ASSET",
            usage="DETAIL",
        )


def statuses(report: Any) -> dict[str, str]:
    return {a.erp_reference: a.status for a in report.accounts}


def row(engine: Engine, reference: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(select(gl_sync).where(gl_sync.c.erp_reference == reference)).one()


def test_first_sync_creates_every_account_parents_first(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()

    report = sync_all(db, erp, fineract)

    assert statuses(report) == dict.fromkeys(["GL-1000", "GL-1100", "GL-2000", "GL-2100"], "SYNCED")
    assert report.counts == {"SYNCED": 4, "SYNC_PENDING": 0, "FAILED": 0}
    assert report.writes == 4
    assert report.in_progress is False
    order = [ref for ref, _, _ in fineract.puts]
    assert order.index("GL-1000") < order.index("GL-1100")
    assert order.index("GL-2000") < order.index("GL-2100")
    bodies = {ref: body for ref, body, _ in fineract.puts}
    assert bodies["GL-1100"]["parentAccount"] == "GL-1000"
    assert "parentAccount" not in bodies["GL-1000"]
    # The Fineract id is kept for our own use only.
    assert row(db, "GL-1100").fineract_gl_id == fineract.synced["GL-1100"][0]
    assert row(db, "GL-1100").attempts == 1
    assert row(db, "GL-1100").synced_at is not None


def test_children_are_synced_after_parents_even_if_the_erp_lists_them_first(db: Engine) -> None:
    accounts = list(reversed(chart()))
    fineract = FakeFineract()

    report = sync_all(db, FakeErp(accounts), fineract)

    assert report.counts["SYNCED"] == 4
    order = [ref for ref, _, _ in fineract.puts]
    assert order.index("GL-1000") < order.index("GL-1100")


def test_repeat_sync_makes_no_fineract_writes(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()
    sync_all(db, erp, fineract)
    fineract.puts.clear()

    report = sync_all(db, erp, fineract)

    assert fineract.puts == []
    assert fineract.gets == []
    assert report.writes == 0
    assert report.counts["SYNCED"] == 4


HASH_A = "a" * 64
HASH_B = "b" * 64


def test_idempotency_key_format() -> None:
    assert idempotency_key("GL-1200", 1, HASH_A) == "GLS:GL-1200:1:aaaaaaaa"
    assert idempotency_key("GL-1200", 12, HASH_A) == "GLS:GL-1200:12:aaaaaaaa"
    long_key = idempotency_key("R" * 100, 3, HASH_A)
    assert len(long_key) <= 50
    assert long_key.startswith("GLS:") and long_key.endswith(":3:aaaaaaaa")
    assert long_key != idempotency_key("R" * 99 + "S", 3, HASH_A)


def test_idempotency_key_changes_with_the_data() -> None:
    # If integration_db is ever rebuilt while Fineract keeps its cache, attempt 1 comes
    # back. Other data must never reuse the cached answer for the old data.
    assert idempotency_key("GL-1200", 1, HASH_A) != idempotency_key("GL-1200", 1, HASH_B)


def test_each_attempt_sends_its_own_key(db: Engine) -> None:
    fineract = FakeFineract()
    fineract.next_put["GL-1000"] = FineractUnavailable("down")
    erp = FakeErp(chart()[:1])
    sync_all(db, erp, fineract)
    sync_all(db, erp, fineract)

    keys = [key for _, _, key in fineract.puts]
    assert [str(k).rsplit(":", 1)[0] for k in keys] == ["GLS:GL-1000:1", "GLS:GL-1000:2"]


def test_fineract_down_then_up(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()
    fineract.down = True

    report = sync_all(db, erp, fineract)

    assert set(statuses(report).values()) == {"SYNC_PENDING"}
    # Once Fineract is unreachable, the run stops calling it.
    assert len(fineract.puts) == 1
    first = row(db, "GL-1000")
    assert first.attempts == 1
    assert "unavailable" in first.last_error.lower()

    fineract.down = False
    fineract.puts.clear()
    report = sync_all(db, erp, fineract)

    assert report.counts == {"SYNCED": 4, "SYNC_PENDING": 0, "FAILED": 0}
    # The account tried before is looked up first, never sent again blindly.
    assert fineract.gets == ["GL-1000"]
    assert row(db, "GL-1000").last_error is None


def test_timeout_then_lookup_finds_it(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    fineract.next_put["GL-1000"] = "timeout_after_write"

    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1000": "SYNCED"}
    assert len(fineract.puts) == 1
    assert fineract.gets == ["GL-1000"]
    assert row(db, "GL-1000").fineract_gl_id == fineract.synced["GL-1000"][0]


def test_timeout_and_failed_lookup_waits_then_looks_up_before_any_retry(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    fineract.next_put["GL-1000"] = "timeout_after_write"
    fineract.lookup_down = True

    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1000": "SYNC_PENDING"}
    assert "did not answer" in row(db, "GL-1000").last_error

    fineract.lookup_down = False
    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1000": "SYNCED"}
    assert len(fineract.puts) == 1  # found by the lookup, not sent again


def test_timeout_and_lookup_finds_nothing_retries_next_run_with_a_new_key(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    fineract.next_put["GL-1000"] = "timeout"

    assert statuses(sync_all(db, erp, fineract)) == {"GL-1000": "SYNC_PENDING"}
    assert statuses(sync_all(db, erp, fineract)) == {"GL-1000": "SYNCED"}
    keys = [key for _, _, key in fineract.puts]
    assert [str(k).rsplit(":", 1)[0] for k in keys] == ["GLS:GL-1000:1", "GLS:GL-1000:2"]
    assert fineract.gets == ["GL-1000", "GL-1000"]


def test_lookup_with_another_gl_code_fails(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    fineract.synced["GL-1000"] = (55, {"glCode": "9999", "name": "Other"})
    fineract.next_put["GL-1000"] = "timeout"

    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1000": "FAILED"}
    assert "9999" in row(db, "GL-1000").last_error


def test_code_conflict_fails_and_children_are_skipped(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()
    fineract.taken_codes.add("1000")

    report = sync_all(db, erp, fineract)

    assert statuses(report) == {
        "GL-1000": "FAILED",
        "GL-1100": "FAILED",
        "GL-2000": "SYNCED",
        "GL-2100": "SYNCED",
    }
    assert "GL_CODE_TAKEN" in row(db, "GL-1000").last_error
    assert row(db, "GL-1100").last_error == "parent GL-1000 not synced"
    assert "GL-1100" not in [ref for ref, _, _ in fineract.puts]
    assert row(db, "GL-1100").attempts == 0


def test_rejected_body_fails_with_the_reason(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    fineract.next_put["GL-1000"] = FineractRejected(
        403, {"userMessageGlobalisationCode": "error.msg.not.authorized"}
    )

    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1000": "FAILED"}
    assert "error.msg.not.authorized" in row(db, "GL-1000").last_error


def test_children_of_a_pending_parent_wait(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:2]), FakeFineract()
    fineract.next_put["GL-1000"] = "timeout"
    fineract.lookup_down = True

    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1000": "SYNC_PENDING", "GL-1100": "SYNC_PENDING"}
    assert row(db, "GL-1100").last_error == "waiting for parent GL-1000"


def test_failed_account_is_tried_again_on_the_next_run(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:2]), FakeFineract()
    fineract.taken_codes.add("1000")
    sync_all(db, erp, fineract)
    fineract.taken_codes.clear()

    report = sync_all(db, erp, fineract)

    assert report.counts["SYNCED"] == 2


def test_synced_account_changed_in_erp_stays_synced_with_a_warning(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()
    sync_all(db, erp, fineract)
    fineract.puts.clear()
    renamed = erp_account("1000", "ASSET", "HEADER", None, "Assets (renamed)")
    child = erp_account("1200", "ASSET", "DETAIL", "1000", "Loan Receivable")
    erp.accounts = [renamed if a.code == "1000" else a for a in erp.accounts] + [child]

    report = sync_all(db, erp, fineract)

    # Fineract still holds it, so it stays SYNCED; the change is only reported.
    assert statuses(report)["GL-1000"] == "SYNCED"
    assert row(db, "GL-1000").last_error == CHANGED_IN_ERP
    assert row(db, "GL-1000").name == "Assets"  # what was synced stays on record
    assert [ref for ref, _, _ in fineract.puts] == ["GL-1200"]  # a new child still syncs
    assert statuses(report)["GL-1200"] == "SYNCED"
    # Changing it back in the ERP clears the warning.
    erp.accounts = [*chart(), child]
    sync_all(db, erp, fineract)
    assert row(db, "GL-1000").last_error is None


def test_never_sent_account_changed_in_erp_is_sent_with_the_new_data(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()
    fineract.down = True
    sync_all(db, erp, fineract)  # only GL-1000 was tried; GL-1100 was never sent
    assert row(db, "GL-1100").attempts == 0
    renamed = erp_account("1100", "ASSET", "DETAIL", "1000", "Main Bank")
    erp.accounts = [renamed if a.code == "1100" else a for a in erp.accounts]
    fineract.down = False

    report = sync_all(db, erp, fineract)

    assert report.counts == {"SYNCED": 4, "SYNC_PENDING": 0, "FAILED": 0}
    assert fineract.synced["GL-1100"][1]["name"] == "Main Bank"
    assert row(db, "GL-1100").name == "Main Bank"


def test_failed_account_corrected_in_erp_is_sent_again(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:2]), FakeFineract()
    fineract.taken_codes.add("1100")
    assert statuses(sync_all(db, erp, fineract))["GL-1100"] == "FAILED"
    fixed = ErpAccount(**{**chart()[1].model_dump(), "code": "1150"})  # same reference

    erp.accounts = [chart()[0], fixed]
    report = sync_all(db, erp, fineract)

    assert statuses(report)["GL-1100"] == "SYNCED"
    assert fineract.synced["GL-1100"][1]["glCode"] == "1150"
    assert row(db, "GL-1100").erp_code == "1150"


def test_unknown_outcome_then_erp_change_is_looked_up_before_anything(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    fineract.next_put["GL-1000"] = "timeout_after_write"  # Fineract stored the old name
    fineract.lookup_down = True
    sync_all(db, erp, fineract)
    erp.accounts = [erp_account("1000", "ASSET", "HEADER", None, "Assets (renamed)")]
    fineract.lookup_down = False
    fineract.puts.clear()

    report = sync_all(db, erp, fineract)

    assert fineract.gets[-1] == "GL-1000"
    assert fineract.puts == []  # never sent blindly
    assert statuses(report) == {"GL-1000": "FAILED"}
    assert "older data" in row(db, "GL-1000").last_error


def test_unknown_outcome_then_erp_change_and_nothing_found_sends_the_new_data(
    db: Engine,
) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    fineract.next_put["GL-1000"] = "timeout"  # nothing stored
    sync_all(db, erp, fineract)
    erp.accounts = [erp_account("1000", "ASSET", "HEADER", None, "Assets (renamed)")]

    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1000": "SYNCED"}
    assert fineract.synced["GL-1000"][1]["name"] == "Assets (renamed)"


def test_missing_parent_fails_then_syncs_once_the_parent_is_in_the_erp(db: Engine) -> None:
    erp, fineract = FakeErp([chart()[1]]), FakeFineract()  # GL-1100 without its parent 1000

    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1100": "FAILED"}
    assert row(db, "GL-1100").last_error == "parent code 1000 is not in the ERP chart"
    assert fineract.puts == []

    erp.accounts = chart()[:2]
    report = sync_all(db, erp, fineract)

    assert statuses(report) == {"GL-1000": "SYNCED", "GL-1100": "SYNCED"}


def test_unreadable_fineract_answer_is_not_copied_into_last_error(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    leak = "Unexpected Fineract answer: input_value={'glAccountId': 98765}"
    fineract.next_put["GL-1000"] = FineractServerError(200, {"glAccountId": 98765}, leak)

    sync_all(db, erp, fineract)

    error = row(db, "GL-1000").last_error
    assert "did not answer" in error
    assert "98765" not in error


def test_rejection_without_codes_does_not_copy_fineract_text(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:1]), FakeFineract()
    fineract.next_put["GL-1000"] = FineractRejected(
        400, {"developerMessage": "glAccountId 98765 is broken"}
    )

    sync_all(db, erp, fineract)

    error = row(db, "GL-1000").last_error
    assert error == "Fineract refused (400): no reason given"


def test_a_second_run_at_the_same_time_does_not_call_anything(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()
    with db.connect() as other:
        assert other.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": GL_SYNC_LOCK_KEY}
        ).scalar()
        try:
            report = sync_all(db, erp, fineract)
        finally:
            other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": GL_SYNC_LOCK_KEY})

    assert report.in_progress is True
    assert report.writes == 0
    assert erp.calls == 0 and fineract.puts == []
    assert sync_all(db, erp, fineract).in_progress is False


def test_report_without_syncing_shows_no_fineract_ids(db: Engine) -> None:
    sync_all(db, FakeErp(chart()), FakeFineract())

    report = get_report(db)

    assert [a.erp_reference for a in report.accounts] == [
        "GL-1000",
        "GL-1100",
        "GL-2000",
        "GL-2100",
    ]
    first = report.accounts[0]
    assert first.code == "1000" and first.name == "Assets" and first.attempts == 1
    assert "fineract_gl_id" not in vars(first)
    assert report.writes == 0


def test_sync_one_syncs_only_that_account(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()

    status = sync_one(db, erp, fineract, "GL-1000")

    assert status.status == "SYNCED"
    assert [ref for ref, _, _ in fineract.puts] == ["GL-1000"]
    assert sync_one(db, erp, fineract, "GL-1100").status == "SYNCED"
    with pytest.raises(LookupError):
        sync_one(db, erp, fineract, "GL-NOPE")


def test_sync_one_while_a_run_holds_the_lock_calls_nothing(db: Engine) -> None:
    erp, fineract = FakeErp(chart()), FakeFineract()
    with db.connect() as other:
        assert other.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": GL_SYNC_LOCK_KEY}
        ).scalar()
        try:
            with pytest.raises(SyncInProgressError):
                sync_one(db, erp, fineract, "GL-1000")
        finally:
            other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": GL_SYNC_LOCK_KEY})

    assert erp.calls == 0
    assert fineract.puts == [] and fineract.gets == []


def test_synced_account_whose_parent_leaves_the_erp_chart_stays_synced(db: Engine) -> None:
    erp, fineract = FakeErp(chart()[:2]), FakeFineract()
    sync_all(db, erp, fineract)
    erp.accounts = [chart()[1]]  # the ERP no longer lists parent 1000

    report = sync_all(db, erp, fineract)

    assert statuses(report)["GL-1100"] == "SYNCED"  # Fineract still holds it
