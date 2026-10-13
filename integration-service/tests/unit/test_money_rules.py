"""Pure rules for disbursements and repayments: request checks, the Fineract bodies, the
answer built from Fineract's own transaction (never an id), Fineract refusals in our words,
and the write itself against a fake Fineract (lookup before any retry)."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from integration_service.clients.errors import (
    FineractError,
    FineractNotFound,
    FineractRejected,
    FineractTimeout,
    FineractUnavailable,
)
from integration_service.errors import ApiError, ErrorCode
from integration_service.mapping.journal import GlRoleAccounts
from integration_service.schemas.money import DisbursementRequest, RepaymentRequest
from integration_service.services.fineract_calls import (
    Done,
    NotSent,
    execute_fineract_write,
)
from integration_service.services.money import (
    LoanNotRepayable,
    build_disbursement_body,
    build_repayment_body,
    check_repayable,
    check_transaction_date,
    disbursement_answer,
    disbursement_rejection,
    disbursement_terms,
    money_plan,
    repayment_answer,
    repayment_payment_type,
    repayment_rejection,
    transaction_lookup,
    transaction_path,
    write_call,
)
from integration_service.services.templates import (
    DisbursementForm,
    RepaymentForm,
    parse_disbursement_form,
    parse_repayment_form,
)

ID_KEY = re.compile(r"(^id$|_id$|Id$)")
LOAN = "LN-2026-000123"
ACCOUNTS = GlRoleAccounts(
    bank="1100", loan_portfolio="1200", interest_income="4100", overpayment_liability="2100"
)


FIXTURES = Path(__file__).parent.parent / "fixtures" / "fineract_templates"
TODAY = date(2026, 10, 13)


def template(name: str, **changes: Any) -> Any:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    data.update(changes)
    return data


def disbursement_form(**changes: Any) -> DisbursementForm:
    return parse_disbursement_form(template("loan_disburse_template.json", **changes))


def repayment_form() -> RepaymentForm:
    return parse_repayment_form(template("loan_repayment_template.json"))


def accounts() -> GlRoleAccounts:
    return ACCOUNTS


def fineract_txn(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": 17,
        "loanId": 4,
        "officeId": 2,
        "type": {"id": 2, "code": "loanTransactionType.repayment", "repayment": True},
        "date": [2026, 10, 13],
        "currency": {"code": "RWF", "decimalPlaces": 0},
        "amount": 100000.0,
        "principalPortion": 82000.0,
        "interestPortion": 18000.0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
        "outstandingLoanBalance": 1118000.0,
        "externalId": "RPY:PAY-1",
        "manuallyReversed": False,
    }
    body.update(changes)
    return body


def disbursement_txn(**changes: Any) -> dict[str, Any]:
    return fineract_txn(
        type={"id": 1, "code": "loanTransactionType.disbursement", "disbursement": True},
        amount=1200000.0,
        principalPortion=0,
        interestPortion=0,
        outstandingLoanBalance=1200000.0,
        externalId="DSB:FIN-1",
        **changes,
    )


def id_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if ID_KEY.search(key) and key != "correlation_id":
                found.append(key)
            found += id_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            found += id_keys(inner)
    return found


def rejected(status: int, *codes: str) -> FineractRejected:
    return FineractRejected(
        status,
        {
            "httpStatusCode": str(status),
            "developerMessage": "Transaction with identifier 1 does not exist",
            "errors": [{"userMessageGlobalisationCode": c} for c in codes],
        },
    )


# --- requests ----------------------------------------------------------------------------------


def test_disbursement_request_takes_a_reference_and_an_optional_date() -> None:
    request = DisbursementRequest.model_validate({"external_reference": "DSB-0001"})
    assert request.transaction_date is None
    dated = DisbursementRequest.model_validate(
        {"external_reference": "DSB-0001", "transaction_date": "2026-10-13"}
    )
    assert dated.transaction_date == date(2026, 10, 13)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"external_reference": "bad ref"},
        {"external_reference": "R" * 41},
        {"external_reference": "DSB-1", "amount": 5},
    ],
)
def test_disbursement_request_refuses_bad_input(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DisbursementRequest.model_validate(body)


@pytest.mark.parametrize("amount", [1000.6, "1000", 0, -5, True, None])
def test_repayment_amount_is_whole_positive_rwf(amount: Any) -> None:
    with pytest.raises(ValidationError):
        RepaymentRequest.model_validate({"external_reference": "PAY-1", "amount": amount})


def test_repayment_request_reads_everything() -> None:
    request = RepaymentRequest.model_validate(
        {
            "external_reference": "PAY-1",
            "amount": 100000,
            "transaction_date": "2026-10-13",
            "payment_type": "Money Transfer",
        }
    )
    assert request.amount == 100000
    assert request.payment_type == "Money Transfer"


def test_repayment_request_refuses_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RepaymentRequest.model_validate(
            {"external_reference": "PAY-1", "amount": 1, "paymentTypeId": 1}
        )


def test_transaction_date_cannot_be_in_the_future() -> None:
    today = date(2026, 10, 13)
    assert check_transaction_date(None, today) == today
    assert check_transaction_date(date(2026, 10, 1), today) == date(2026, 10, 1)
    with pytest.raises(ApiError) as caught:
        check_transaction_date(date(2026, 10, 14), today)
    assert caught.value.status == 422
    assert caught.value.code == ErrorCode.VALIDATION_ERROR
    assert caught.value.details == {
        "problems": [
            {"field": "transaction_date", "problem": "cannot be in the future (Kigali date)"}
        ]
    }


# --- what Fineract's templates decide ----------------------------------------------------------


def test_disbursed_amount_is_the_templates_approved_amount() -> None:
    """Fineract's disbursement template holds the approved principal. That is what is paid
    out, whatever principal the loan was applied for."""
    form = disbursement_form(amount=750000)
    assert disbursement_terms(form, None, TODAY)[1] == 750000


@pytest.mark.parametrize(
    ("expected", "today", "chosen"),
    [
        ([2026, 10, 20], date(2026, 10, 13), date(2026, 10, 13)),  # expected later: today
        ([2026, 10, 20], date(2026, 10, 25), date(2026, 10, 20)),  # expected passed: expected
        ([2026, 10, 13], date(2026, 10, 13), date(2026, 10, 13)),
        (None, date(2026, 10, 13), date(2026, 10, 13)),  # no expected date: today
    ],
)
def test_default_disbursement_date_is_the_expected_date_never_later_than_today(
    expected: list[int] | None, today: date, chosen: date
) -> None:
    form = disbursement_form(date=expected)
    assert disbursement_terms(form, None, today)[0] == chosen


def test_a_disbursement_date_sent_by_the_partner_wins() -> None:
    form = disbursement_form(date=[2026, 10, 1])
    assert disbursement_terms(form, date(2026, 10, 5), TODAY)[0] == date(2026, 10, 5)


def test_a_future_disbursement_date_is_refused_by_the_template_check() -> None:
    with pytest.raises(ApiError) as caught:
        disbursement_terms(disbursement_form(), date(2026, 10, 14), TODAY)
    assert caught.value.status == 422
    assert caught.value.details == {
        "problems": [
            {"field": "transaction_date", "problem": "cannot be in the future (Kigali date)"}
        ]
    }


def test_partner_payment_type_is_found_by_name_any_case() -> None:
    form = repayment_form()
    assert repayment_payment_type(form, 1000, None, "money transfer", TODAY) == 1
    assert repayment_payment_type(form, 1000, None, "Money Transfer", TODAY) == 1
    assert repayment_payment_type(form, 1000, None, None, TODAY) is None


@pytest.mark.parametrize(
    "name", ["Repayment Adjustment Refund", "Repayment Adjustment Chargeback", "Cheque"]
)
def test_system_defined_or_unknown_payment_types_are_refused(name: str) -> None:
    """System payment types are for Fineract's own adjustments; only partner ones are taken."""
    with pytest.raises(ApiError) as caught:
        repayment_payment_type(repayment_form(), 1000, None, name, TODAY)
    error = caught.value
    assert (error.code, error.status) == (ErrorCode.VALIDATION_ERROR, 422)
    assert error.details == {
        "problems": [{"field": "payment_type", "problem": "must be one of: Money Transfer"}]
    }
    assert "Refund" not in error.message and "Chargeback" not in error.message


def test_repayment_problems_are_all_listed() -> None:
    with pytest.raises(ApiError) as caught:
        repayment_payment_type(repayment_form(), 1000, date(2026, 10, 14), "Gold Bars", TODAY)
    assert caught.value.details is not None
    assert [p["field"] for p in caught.value.details["problems"]] == [
        "transaction_date",
        "payment_type",
    ]


# --- Fineract bodies ---------------------------------------------------------------------------


def test_disbursement_body_names_the_transaction_by_our_op_key() -> None:
    assert build_disbursement_body(date(2026, 10, 13), 1200000, "DSB:FIN-1") == {
        "actualDisbursementDate": "2026-10-13",
        "transactionAmount": 1200000,
        "externalId": "DSB:FIN-1",
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }


def test_repayment_body_with_and_without_a_payment_type() -> None:
    body = build_repayment_body(date(2026, 10, 13), 100000, "RPY:PAY-1")
    assert body == {
        "transactionDate": "2026-10-13",
        "transactionAmount": 100000,
        "externalId": "RPY:PAY-1",
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }
    typed = build_repayment_body(date(2026, 10, 13), 100000, "RPY:PAY-1", 1)
    assert typed["paymentTypeId"] == 1


def test_transaction_path_quotes_our_references() -> None:
    assert (
        transaction_path(LOAN, "RPY:PAY.1_x")
        == "/loans/external-id/LN-2026-000123/transactions/external-id/RPY%3APAY.1_x"
    )


# --- what the partner gets ---------------------------------------------------------------------


def test_disbursement_answer_comes_from_fineracts_transaction() -> None:
    txn = disbursement_txn()
    plan = money_plan("DSB", "DSB:FIN-1", LOAN, txn, accounts)
    body = disbursement_answer("FIN-1", LOAN, "SME-000001", txn, plan, "ACTIVE")
    assert body == {
        "loan_reference": LOAN,
        "client_uid": "SME-000001",
        "external_reference": "FIN-1",
        "type": "DISBURSEMENT",
        "status": "ERP_PENDING",
        "amount": 1200000,
        "transaction_date": "2026-10-13",
        "erp_status": "PENDING",
        "loan_status": "ACTIVE",
    }
    assert id_keys(body) == []


def test_repayment_answer_shows_fineracts_split() -> None:
    """outstanding_after is the loan summary's figure, never the transaction's running
    balance, which Fineract rewrites when it reprocesses same-day repayments."""
    txn = fineract_txn()
    plan = money_plan("RPY", "RPY:PAY-1", LOAN, txn, accounts)
    body = repayment_answer("PAY-1", LOAN, "SME-000001", txn, plan, "ACTIVE", 949839)
    assert body == {
        "loan_reference": LOAN,
        "client_uid": "SME-000001",
        "external_reference": "PAY-1",
        "type": "REPAYMENT",
        "status": "ERP_PENDING",
        "amount": 100000,
        "transaction_date": "2026-10-13",
        "principal_portion": 82000,
        "interest_portion": 18000,
        "overpayment_portion": 0,
        "fees_portion": 0,
        "penalties_portion": 0,
        "outstanding_after": 949839,
        "erp_status": "PENDING",
        "loan_status": "ACTIVE",
    }
    assert id_keys(body) == []


def test_a_fee_in_the_split_is_answered_but_needs_reconciliation() -> None:
    txn = fineract_txn(amount=101000.0, feeChargesPortion=1000.0)
    plan = money_plan("RPY", "RPY:PAY-1", LOAN, txn, accounts)
    body = repayment_answer("PAY-1", LOAN, "SME-000001", txn, plan, None, None)
    assert plan.entry is None
    assert body["status"] == "NEEDS_RECONCILIATION"
    assert body["erp_status"] == "NEEDS_RECONCILIATION"
    assert body["fees_portion"] == 1000


def test_plan_holds_dr_1200_cr_1100_for_a_disbursement() -> None:
    plan = money_plan("DSB", "DSB:FIN-1", LOAN, disbursement_txn(), accounts)
    assert plan.entry is not None
    lines = plan.entry.to_erp_body()["lines"]
    assert lines == [
        {"account_code": "1200", "debit": 1200000},
        {"account_code": "1100", "credit": 1200000},
    ]


def test_plan_never_raises_on_bad_data_or_missing_accounts() -> None:
    assert money_plan("RPY", "RPY:PAY-1", LOAN, {"nonsense": 1}, accounts).entry is None

    def broken() -> GlRoleAccounts:
        from integration_service.mapping.journal import GlRolesConfigError

        raise GlRolesConfigError("missing")

    plan = money_plan("RPY", "RPY:PAY-1", LOAN, fineract_txn(), broken)
    assert plan.entry is None
    assert plan.problem == "GL role accounts could not be read"


# --- refusals ----------------------------------------------------------------------------------


def test_second_disbursement_refused_by_fineract_is_already_disbursed() -> None:
    error = rejected(403, "error.msg.loan.disburse.amount.must.be.less.than.approved.principal")
    api = disbursement_rejection(error, LOAN, loan_status_id=300)
    assert (api.code, api.status) == (ErrorCode.LOAN_ALREADY_DISBURSED, 409)
    assert "identifier" not in api.message


@pytest.mark.parametrize(
    ("status_id", "code", "http"),
    [
        (300, ErrorCode.LOAN_ALREADY_DISBURSED, 409),
        (600, ErrorCode.LOAN_ALREADY_DISBURSED, 409),
        (100, ErrorCode.VALIDATION_ERROR, 422),
        (200, ErrorCode.VALIDATION_ERROR, 422),
        (None, ErrorCode.LOAN_ALREADY_DISBURSED, 409),
    ],
)
def test_wrong_state_refusal_depends_on_the_loan(
    status_id: int | None, code: ErrorCode, http: int
) -> None:
    error = rejected(400, "error.msg.loan.disbursal.account.is.not.approve.not.disbursed.state")
    api = disbursement_rejection(error, LOAN, loan_status_id=status_id)
    assert (api.code, api.status) == (code, http)


def test_transaction_external_id_used_elsewhere_is_a_conflict() -> None:
    error = rejected(400, "validation.msg.loan.transaction.externalId.value.must.be.unique")
    for api in (
        disbursement_rejection(error, LOAN, loan_status_id=200),
        repayment_rejection(error, LOAN),
    ):
        assert (api.code, api.status) == (ErrorCode.IDEMPOTENCY_CONFLICT, 409)


def test_repayment_on_a_loan_not_paid_out_is_422() -> None:
    api = repayment_rejection(
        rejected(400, "error.msg.loan.must.be.active.fully.paid.or.overpaid"), LOAN
    )
    assert (api.code, api.status) == (ErrorCode.VALIDATION_ERROR, 422)


def test_unmapped_refusal_never_quotes_fineract() -> None:
    api = repayment_rejection(rejected(403, "error.msg.something.new"), LOAN)
    assert api.status == 422
    assert "identifier" not in api.message
    assert api.details == {"fineract_codes": ["error.msg.something.new"]}


@pytest.mark.parametrize(
    ("status_id", "ok"),
    [(300, True), (100, False), (200, False), (600, False), (700, False), (None, False)],
)
def test_only_an_active_loan_takes_repayments(status_id: int | None, ok: bool) -> None:
    loan = {"status": {"id": status_id, "code": "x"}} if status_id else {}
    if ok:
        check_repayable(loan, LOAN)
    else:
        with pytest.raises(LoanNotRepayable) as caught:
            check_repayable(loan, LOAN)
        assert caught.value.error.status == 422


# --- the write, against a fake Fineract -------------------------------------------------------


class FakeFineract:
    """Records every call. posts and gets are queues of answers (dict, None for 404, or an
    exception to raise)."""

    def __init__(self, posts: list[Any], gets: list[Any]) -> None:
        self.posts = posts
        self.gets = gets
        self.post_keys: list[str | None] = []
        self.get_paths: list[str] = []

    def get(
        self, path: str, params: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> Any:
        self.get_paths.append(path)
        return self._answer(self.gets.pop(0), path)

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.post_keys.append(idempotency_key)
        return self._answer(self.posts.pop(0), path)

    @staticmethod
    def _answer(answer: Any, path: str) -> Any:
        if isinstance(answer, FineractError):
            raise answer
        if answer is None:
            raise FineractNotFound(404, {"userMessageGlobalisationCode": "not.found"})
        return answer


def disburse(fake: FakeFineract, attempt: int) -> Any:
    call = write_call(
        fake,
        f"/loans/external-id/{LOAN}",
        {"command": "disburse"},
        {"externalId": "DSB:FIN-1"},
        None,
    )
    lookup = transaction_lookup(fake, LOAN, "DSB:FIN-1", None)
    return execute_fineract_write(call, lookup, "DSB:FIN-1", attempt, check_first=attempt > 1)


def test_timeout_is_looked_up_and_the_write_runs_once() -> None:
    fake = FakeFineract(posts=[FineractTimeout("slow")], gets=[disbursement_txn()])
    outcome = disburse(fake, 1)
    assert isinstance(outcome, Done)
    assert outcome.found_by_lookup
    assert outcome.result["externalId"] == "DSB:FIN-1"
    assert fake.post_keys == ["DSB:FIN-1:1"]
    assert fake.get_paths == [transaction_path(LOAN, "DSB:FIN-1")]


def test_not_sent_then_a_retry_uses_attempt_two_and_looks_first() -> None:
    fake = FakeFineract(
        posts=[FineractUnavailable("down"), {"resourceId": 9, "subResourceId": 17}],
        gets=[None],
    )
    first = disburse(fake, 1)
    assert isinstance(first, NotSent)
    assert fake.get_paths == []  # not sent: nothing to look up

    second = disburse(fake, 2)
    assert isinstance(second, Done)
    assert not second.found_by_lookup
    assert fake.post_keys == ["DSB:FIN-1:1", "DSB:FIN-1:2"]
    assert fake.get_paths == [transaction_path(LOAN, "DSB:FIN-1")]  # looked up before sending


def test_a_retry_that_finds_the_earlier_attempt_sends_nothing() -> None:
    fake = FakeFineract(posts=[], gets=[disbursement_txn()])
    outcome = disburse(fake, 2)
    assert isinstance(outcome, Done)
    assert outcome.found_by_lookup
    assert fake.post_keys == []


def test_lookup_of_a_missing_transaction_is_none() -> None:
    fake = FakeFineract(posts=[], gets=[None])
    assert transaction_lookup(fake, LOAN, "RPY:PAY-1", None)() is None
