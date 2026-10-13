"""POST /api/loans/{loan_reference}/repayments against the running Fineract (as the
least-privilege API user) and the running Mock ERP. The split in every answer and every
journal entry must be Fineract's own. Fixtures and helpers come from test_disburse_api."""

# Fixtures imported from test_disburse_api are used as test arguments of the same name.
# ruff: noqa: F811

from __future__ import annotations

from typing import Any

from flask.testing import FlaskClient
from sqlalchemy import Engine
from test_disburse_api import (  # noqa: F401 (fixtures are used by name)
    PARTNER_A,
    Flaky,
    LandsLate,
    _clean,
    accounts,
    admin,
    checked,
    disburse,
    erp,
    fineract,
    fineract_transactions,
    history,
    new_loan,
    outbox_entry,
    settings,
    tag,
)

from integration_service.clients.erp import ErpClient
from integration_service.clients.errors import FineractTimeout, FineractUnavailable
from integration_service.clients.fineract import FineractClient
from integration_service.errors import ApiError
from integration_service.schemas.money import RepaymentRequest
from integration_service.services.loans import kigali_today
from integration_service.services.money import record_repayment, transaction_path
from integration_service.services.operations import get_operation
from integration_service.services.outbox import send_due
from integration_service.services.recovery import recover_once


def active_loan(client: FlaskClient, auth: dict[str, str]) -> str:
    loan = new_loan(client, auth)
    paid_out = disburse(client, auth, loan, f"IT-DSB-{tag()}")
    assert paid_out.status_code == 200, paid_out.get_json()
    return loan


def repay(
    client: FlaskClient, auth: dict[str, str], loan: str, ref: str, amount: Any, **extra: Any
) -> Any:
    return checked(
        client.post(
            f"/api/loans/{loan}/repayments",
            json={"external_reference": ref, "amount": amount, **extra},
            headers=auth,
        )
    )


def principal_outstanding(client: FlaskClient, auth: dict[str, str], loan: str) -> int:
    """The principal still owed, as Fineract's loan summary shows it now."""
    response = client.get(f"/api/loans/{loan}", headers=auth)
    assert response.status_code == 200
    return int(response.get_json()["balances"]["principal_outstanding"])


def run_repay(db: Engine, fake: Any, loan: str, ref: str, amount: int) -> tuple[int, Any]:
    try:
        answer = record_repayment(
            db,
            fake,
            loan,
            RepaymentRequest(external_reference=ref, amount=amount),
            PARTNER_A,
            None,
            accounts,
        )
    except ApiError as exc:
        return exc.status, {"code": exc.code, "message": exc.message, "details": exc.details}
    return answer.status, answer.body


def test_repayment_split_and_entry_are_fineracts_own(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    response = repay(client, auth, loan, ref, 100000)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()

    txn = fineract.get(transaction_path(loan, f"RPY:{ref}"))
    principal, interest = int(txn["principalPortion"]), int(txn["interestPortion"])
    assert principal + interest == 100000
    assert interest > 0  # interest first: the split is Fineract's, not ours
    assert body == {
        "loan_reference": loan,
        "client_uid": body["client_uid"],
        "external_reference": ref,
        "type": "REPAYMENT",
        "status": "ERP_PENDING",
        "amount": 100000,
        "transaction_date": kigali_today().isoformat(),
        "principal_portion": principal,
        "interest_portion": interest,
        "overpayment_portion": 0,
        "fees_portion": 0,
        "penalties_portion": 0,
        "outstanding_after": principal_outstanding(client, auth, loan),
        "erp_status": "PENDING",
        "loan_status": "ACTIVE",
    }

    row = outbox_entry(db, f"RPY:{ref}")
    assert row["entry"]["source"] == "REPAYMENT"
    assert row["entry"]["lines"] == [
        {"account_code": "1100", "debit": 100000},
        {"account_code": "1200", "credit": principal},
        {"account_code": "4100", "credit": interest},
    ]
    send_due(db, erp)
    saved = erp.get_journal_entry(f"RPY:{ref}")
    assert saved is not None and saved["total"] == 100000
    assert history(client, auth, ref)["erp_status"] == "SENT"


def test_replay_is_identical_and_other_data_is_a_conflict(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    first = repay(client, auth, loan, ref, 50000)
    assert first.status_code == 200
    again = repay(client, auth, loan, ref, 50000)
    assert again.status_code == 200
    assert again.get_json() == first.get_json()
    changed = repay(client, auth, loan, ref, 60000)
    assert changed.status_code == 409
    assert changed.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1


def test_many_references_are_many_repayments(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = active_loan(client, auth)
    for amount in (10000, 20000, 30000):
        assert repay(client, auth, loan, f"IT-RPY-{tag()}", amount).status_code == 200
    amounts = sorted(int(t["amount"]) for t in fineract_transactions(fineract, loan, "repayment"))
    assert amounts == [10000, 20000, 30000]


def test_a_loan_not_paid_out_takes_no_repayment(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = new_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    response = repay(client, auth, loan, ref, 10000)
    assert response.status_code == 422, response.get_json()
    assert response.get_json()["error"]["details"]["loan_status"] == "APPROVED"
    assert history(client, auth, ref)["status"] == "FAILED"
    assert fineract_transactions(fineract, loan, "repayment") == []


def test_payoff_closes_the_loan_and_a_later_repayment_is_refused(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    loan = active_loan(client, auth)
    payoff = client.get(f"/api/loans/{loan}", headers=auth).get_json()["payoff_amount"]
    assert payoff > 1200000
    closing = repay(client, auth, loan, f"IT-RPY-{tag()}", payoff)
    assert closing.status_code == 200, closing.get_json()
    body = closing.get_json()
    assert body["loan_status"] == "CLOSED_OBLIGATIONS_MET"
    assert body["outstanding_after"] == 0
    assert body["principal_portion"] + body["interest_portion"] == payoff

    # Fineract would take this and turn it into an overpayment. We refuse it first.
    late = f"IT-RPY-{tag()}"
    response = repay(client, auth, loan, late, 5000)
    assert response.status_code == 422
    assert response.get_json()["error"]["details"]["loan_status"] == "CLOSED_OBLIGATIONS_MET"
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1


def test_paying_more_than_owed_shows_fineracts_overpayment(
    client: FlaskClient, auth: dict[str, str], db: Engine
) -> None:
    loan = active_loan(client, auth)
    payoff = client.get(f"/api/loans/{loan}", headers=auth).get_json()["payoff_amount"]
    ref = f"IT-RPY-{tag()}"
    response = repay(client, auth, loan, ref, payoff + 5000)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["overpayment_portion"] == 5000
    assert body["loan_status"] == "OVERPAID"
    lines = outbox_entry(db, f"RPY:{ref}")["entry"]["lines"]
    assert {"account_code": "2100", "credit": 5000} in lines
    assert lines[0] == {"account_code": "1100", "debit": payoff + 5000}


def test_payment_type_by_name(client: FlaskClient, auth: dict[str, str]) -> None:
    loan = active_loan(client, auth)
    named = repay(client, auth, loan, f"IT-RPY-{tag()}", 10000, payment_type="Money Transfer")
    assert named.status_code == 200, named.get_json()
    ref = f"IT-RPY-{tag()}"
    unknown = repay(client, auth, loan, ref, 10000, payment_type="Gold Bars")
    assert unknown.status_code == 422
    assert "payment_type" in unknown.get_json()["error"]["message"]
    assert unknown.get_json()["error"]["details"]["problems"][0]["field"] == "payment_type"
    assert history(client, auth, ref)["status"] == "FAILED"


def test_a_system_defined_payment_type_is_refused_and_nothing_is_sent(
    client: FlaskClient, auth: dict[str, str], fineract: FineractClient
) -> None:
    """Fineract lists its own adjustment types (system defined) next to the partner ones.
    Only partner payment types are taken."""
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    response = repay(client, auth, loan, ref, 10000, payment_type="Repayment Adjustment Refund")
    assert response.status_code == 422, response.get_json()
    error = response.get_json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["problems"] == [
        {"field": "payment_type", "problem": "must be one of: Money Transfer"}
    ]
    assert history(client, auth, ref)["status"] == "FAILED"
    assert fineract_transactions(fineract, loan, "repayment") == []


def test_a_failed_template_read_is_503_failed_and_nothing_is_sent(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    """The repayment template is read for every repayment (it holds the currency step and
    the payment types). If that read fails, nothing was sent."""
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    fake = Flaky(fineract, fail_templates=1)
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 503, body
    assert "Nothing was sent" in body["message"]
    op = get_operation(db, "RPY", ref)
    assert op is not None and op.status == "FAILED"
    assert fake.template_reads == 1
    assert fake.post_keys == []
    assert fineract_transactions(fineract, loan, "repayment") == []


def test_bad_amounts_are_422_and_save_nothing(client: FlaskClient, auth: dict[str, str]) -> None:
    for amount in (1000.6, "1000", 0, -1):
        ref = f"IT-RPY-{tag()}"
        response = repay(client, auth, "LN-2026-999999", ref, amount)
        assert response.status_code == 422
        assert client.get(f"/api/transactions/{ref}", headers=auth).status_code == 404


def test_unknown_loan_is_404(client: FlaskClient, auth: dict[str, str]) -> None:
    response = repay(client, auth, "LN-2026-999999", f"IT-RPY-{tag()}", 1000)
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "LOAN_NOT_FOUND"


def test_outstanding_after_is_the_loans_balance_after_each_same_day_repayment(
    client: FlaskClient, auth: dict[str, str]
) -> None:
    """Fineract rewrites a transaction's running balance when it reprocesses same-day
    repayments, so the answer must use the loan's own summary, read right after."""
    loan = active_loan(client, auth)
    for amount in (100000, 200000, 300000):
        answer = repay(client, auth, loan, f"IT-RPY-{tag()}", amount)
        assert answer.status_code == 200, answer.get_json()
        assert answer.get_json()["outstanding_after"] == principal_outstanding(client, auth, loan)
    payoff = client.get(f"/api/loans/{loan}", headers=auth).get_json()["payoff_amount"]
    closing = repay(client, auth, loan, f"IT-RPY-{tag()}", payoff)
    assert closing.get_json()["loan_status"] == "CLOSED_OBLIGATIONS_MET"
    assert closing.get_json()["outstanding_after"] == 0


# --- failures, with the real Fineract behind a stand-in -------------------------------------


def test_not_sent_is_failed_and_the_same_reference_then_works_with_attempt_two(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    fake = Flaky(fineract, FineractUnavailable("down (test)"), fail_posts=1)
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 503, body
    op = get_operation(db, "RPY", ref)
    assert op is not None and op.status == "FAILED"
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 200, body
    assert fake.post_keys == [f"RPY:{ref}:1", f"RPY:{ref}:2"]
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1


def test_a_failed_loan_read_before_the_write_is_not_sent(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    """The ACTIVE check reads the loan before the write. If that read fails, nothing was
    sent, so we say so (FAILED, 503) instead of waiting for recovery."""
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    fake = Flaky(fineract, fail_gets=1)
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 503, body
    op = get_operation(db, "RPY", ref)
    assert op is not None and op.status == "FAILED"
    assert fake.post_keys == []


def test_lost_answer_is_looked_up_and_not_sent_again(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    fake = Flaky(fineract, FineractTimeout("answer lost (test)"), fail_posts=1, after_send=True)
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 200, body
    assert body["amount"] == 50000
    assert fake.post_keys == [f"RPY:{ref}:1"]
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1


def test_unclear_with_a_failed_lookup_is_504_then_202_then_recovery_finishes_it(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    # The loan read before the write passes; the write's answer is lost; the lookup fails.
    fake = Flaky(fineract, FineractTimeout("answer lost (test)"), fail_posts=1, after_send=True)
    original_post = fake.post

    def post_then_fail_next_get(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_post(*args, **kwargs)
        finally:
            fake.fail_gets = 1

    fake.post = post_then_fail_next_get  # type: ignore[method-assign]
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 504, body
    assert repay(client, auth, loan, ref, 50000).status_code == 202

    report = recover_once(
        db, fineract, erp, accounts=accounts, stale_after_seconds=0, send_outbox=False
    )
    assert report.operations.completed == 1
    replay = repay(client, auth, loan, ref, 50000)
    assert replay.status_code == 200
    body = replay.get_json()
    assert body["amount"] == 50000
    assert body["principal_portion"] + body["interest_portion"] == 50000
    assert body["outstanding_after"] == principal_outstanding(client, auth, loan)
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1


def test_unclear_and_not_found_yet_stays_pending_until_recovery_decides(
    client: FlaskClient,
    auth: dict[str, str],
    db: Engine,
    fineract: FineractClient,
    erp: ErpClient,
) -> None:
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    fake = Flaky(fineract, FineractTimeout("no answer (test)"), fail_posts=1)
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 504, body
    assert "nothing was done" not in body["message"]
    op = get_operation(db, "RPY", ref)
    assert op is not None and op.status == "FINERACT_PENDING"
    report = recover_once(
        db, fineract, erp, accounts=accounts, stale_after_seconds=0, send_outbox=False
    )
    assert report.operations.failed == 1
    assert run_repay(db, fineract, loan, ref, 50000)[0] == 200
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1


def test_an_earlier_attempt_that_lands_late_is_ours_not_a_conflict(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    fake = LandsLate(fineract)
    assert run_repay(db, fake, loan, ref, 50000)[0] == 503
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 200, body
    assert body["amount"] == 50000
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1
    assert get_operation(db, "RPY", ref).status == "ERP_PENDING"  # type: ignore[union-attr]


def test_a_payoff_that_lands_late_is_found_not_refused_as_closed(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    """Attempt 1 of a payoff lands just before attempt 2's ACTIVE check: the loan now looks
    closed. It is our own payoff, so it is found and finished, not refused with 422."""
    loan = active_loan(client, auth)
    payoff = client.get(f"/api/loans/{loan}", headers=auth).get_json()["payoff_amount"]
    ref = f"IT-RPY-{tag()}"
    fake = LandsLate(fineract, when="loan_read")
    assert run_repay(db, fake, loan, ref, payoff)[0] == 503
    status, body = run_repay(db, fake, loan, ref, payoff)
    assert status == 200, body
    assert body["loan_status"] == "CLOSED_OBLIGATIONS_MET"
    assert body["outstanding_after"] == 0
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1
    assert fake.post_keys == [f"RPY:{ref}:1"]  # attempt 2 itself was never sent


def test_a_retry_looks_up_the_earlier_attempt_before_reading_the_template(
    client: FlaskClient, auth: dict[str, str], db: Engine, fineract: FineractClient
) -> None:
    """Attempt 1 reached Fineract although we heard "not reachable". On the retry the
    template read fails: the earlier repayment is found first and finished."""
    loan = active_loan(client, auth)
    ref = f"IT-RPY-{tag()}"
    fake = Flaky(fineract, FineractUnavailable("lost (test)"), fail_posts=1, after_send=True)
    assert run_repay(db, fake, loan, ref, 50000)[0] == 503
    reads = fake.template_reads

    fake.fail_templates = 1
    status, body = run_repay(db, fake, loan, ref, 50000)
    assert status == 200, body
    assert body["amount"] == 50000
    assert fake.template_reads == reads
    assert fake.post_keys == [f"RPY:{ref}:1"]
    assert len(fineract_transactions(fineract, loan, "repayment")) == 1
