"""Money movement: disbursement and repayment (Parts E, F and H).

Both follow the steps in services/loans.py, with two additions:
- The answer and the journal entry come from Fineract's own transaction, looked up by its
  externalId (our op key) after Fineract said yes. Never from the request.
- The operation is finished through outbox.record_money_done: FINERACT_DONE, the saved
  answer and the outbox row in one database transaction (ERP_PENDING), or
  NEEDS_RECONCILIATION when the entry cannot be built without guessing.

A loan is paid out once (ARCHITECTURE L1-L5): the saved answer per reference, the one-live-
disbursement index (another reference gets 409 LOAN_ALREADY_DISBURSED, even while the first
is still running), the per-loan advisory lock around the Fineract write, Fineract's unique
transaction externalId and Idempotency-Key, and Fineract's own loan state (a second
disbursement is refused; we answer 409 LOAN_ALREADY_DISBURSED).

A refusal is not always a refusal: on a retry (or when Fineract says our transaction
externalId is taken), our own earlier attempt may have landed late. The transaction under
our op key is ours, so it is looked up before the operation is called FAILED.

Fineract's templates decide the terms, read once before the write (never inside it):
- the disbursement template gives the amount (the approved principal) and the default date
  (the expected disbursement date, but never later than today);
- the repayment template gives the currency step and the payment types. Only partner payment
  types are taken; Fineract's system-defined ones (its own adjustments) are refused.
A failed template read means nothing was sent: FAILED and 503.

Repayments are taken only while Fineract shows the loan ACTIVE. Fineract itself accepts a
repayment on a closed loan and turns the whole amount into an overpayment; we refuse it.
The check runs under the per-loan lock, just before the write.

Fineract's numeric ids go to our tables and logs, never into an answer."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import Engine

from integration_service.clients.errors import (
    FineractError,
    FineractNotFound,
    FineractRejected,
    FineractUnavailable,
)
from integration_service.errors import ApiError, ErrorCode
from integration_service.logs import log_event
from integration_service.mapping.journal import (
    GlRoleAccounts,
    GlRolesConfigError,
    JournalPlan,
    NeedsReconciliation,
    parse_transaction,
    plan_entry,
)
from integration_service.schemas.money import DisbursementRequest, RepaymentRequest
from integration_service.services.fineract_calls import (
    Done,
    Rejected,
    Unknown,
    execute_fineract_write,
)
from integration_service.services.loan_locks import LoanLockTimeoutError, loan_lock
from integration_service.services.loans import (
    FINERACT_DATE_FORMAT,
    FINERACT_LOCALE,
    Answer,
    FineractApi,
    OurLoan,
    answer_for_begin,
    fail,
    find_loan,
    fineract_date,
    fineract_refusal,
    kigali_today,
    loan_status_code,
    our_loan,
    settle,
    whole_rwf,
)
from integration_service.services.operations import (
    TYPE_NAMES,
    InProgress,
    New,
    Operation,
    begin,
    get_operation,
    mark_failed,
    set_fields,
)
from integration_service.services.outbox import record_money_done
from integration_service.services.templates import (
    DisbursementForm,
    FieldProblem,
    RepaymentForm,
    check_disbursement,
    check_repayment,
    fetch_disbursement_form,
    fetch_repayment_form,
    validation_error,
)
from integration_service.state_machine import ERP_PENDING, FINERACT_PENDING, NEEDS_RECONCILIATION

Accounts = Callable[[], GlRoleAccounts]

ACTIVE_STATUS_ID = 300
NOT_APPROVED_STATUS_ID = 100
APPROVED_STATUS_ID = 200
DISBURSE_OVER_APPROVED = "error.msg.loan.disburse.amount.must.be.less.than.approved.principal"
DISBURSE_WRONG_STATE = "error.msg.loan.disbursal.account.is.not.approve.not.disbursed.state"
TXN_EXTERNAL_ID_TAKEN = "validation.msg.loan.transaction.externalId.value.must.be.unique"
REPAY_NOT_ACTIVE = "error.msg.loan.must.be.active.fully.paid.or.overpaid"


class LoanNotRepayable(Exception):  # noqa: N818 (a refusal, not a fault)
    """Raised from inside the write, before anything is sent: the loan is not ACTIVE."""

    def __init__(self, error: ApiError) -> None:
        super().__init__(error.message)
        self.error = error


# --- pure rules -------------------------------------------------------------------------------


def check_transaction_date(requested: date | None, today: date) -> date:
    """The early check, before anything is saved: the request's date, or today (Kigali).
    Never in the future. The template checks run again once the template is read."""
    chosen = requested or today
    if chosen > today:
        raise validation_error(
            [FieldProblem("transaction_date", "cannot be in the future (Kigali date)")]
        )
    return chosen


def disbursement_terms(
    form: DisbursementForm, requested: date | None, today: date
) -> tuple[date, int]:
    """The date and amount to pay out, from Fineract's disbursement template: the partner's
    date or the template's default, and the approved amount. Raises a 422 ApiError."""
    problems = check_disbursement(form, requested, today)
    if problems:
        raise validation_error(problems)
    return requested or form.default_date(today), form.amount


def repayment_payment_type(
    form: RepaymentForm,
    amount: int,
    requested: date | None,
    payment_type: str | None,
    today: date,
) -> int | None:
    """Checks a repayment against Fineract's repayment template; the Fineract id of the
    partner payment type (None when none was named). Raises a 422 ApiError."""
    problems = check_repayment(form, amount, requested, payment_type, today)
    if problems:
        raise validation_error(problems)
    return form.payment_type_id(payment_type) if payment_type is not None else None


def build_disbursement_body(transaction_date: date, amount: int, op_key: str) -> dict[str, Any]:
    return {
        "actualDisbursementDate": transaction_date.isoformat(),
        "transactionAmount": amount,
        "externalId": op_key,
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": FINERACT_LOCALE,
    }


def build_repayment_body(
    transaction_date: date, amount: int, op_key: str, payment_type: int | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "transactionDate": transaction_date.isoformat(),
        "transactionAmount": amount,
        "externalId": op_key,
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": FINERACT_LOCALE,
    }
    if payment_type is not None:
        body["paymentTypeId"] = payment_type
    return body


def loan_path(loan_reference: str) -> str:
    return f"/loans/external-id/{quote(loan_reference, safe='')}"


def transaction_path(loan_reference: str, op_key: str) -> str:
    return f"{loan_path(loan_reference)}/transactions/external-id/{quote(op_key, safe='')}"


def check_repayable(loan: Mapping[str, Any], loan_reference: str) -> None:
    """Only an ACTIVE loan takes repayments. Raises LoanNotRepayable otherwise."""
    status = loan.get("status") or {}
    if status.get("id") == ACTIVE_STATUS_ID:
        return
    code = loan_status_code(status.get("code"))
    raise LoanNotRepayable(
        ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Loan {loan_reference} takes repayments only while it is ACTIVE; "
            f"Fineract shows it {code}.",
            422,
            details={"loan_status": code},
        )
    )


def money_plan(
    op_type: str, op_key: str, loan_reference: str, found: Any, accounts: Accounts
) -> JournalPlan:
    """The journal plan for Fineract's transaction. Never raises: Fineract has done its part
    and that must be recorded, so bad data or a missing GL file give a plan without an
    entry (NEEDS_RECONCILIATION)."""
    try:
        txn = parse_transaction(found)
        roles = accounts()
    except NeedsReconciliation as exc:
        return JournalPlan(op_key, None, exc.reason)
    except GlRolesConfigError:
        return JournalPlan(op_key, None, "GL role accounts could not be read")
    return plan_entry(op_type, op_key, txn, roles, loan_reference)


def _status_after(plan: JournalPlan) -> str:
    return ERP_PENDING if plan.entry is not None else NEEDS_RECONCILIATION


def _date(value: Any) -> str | None:
    found = fineract_date(value)
    return found.isoformat() if found else None


def disbursement_answer(
    external_reference: str,
    loan_reference: str,
    client_uid: str | None,
    txn: Mapping[str, Any],
    plan: JournalPlan,
    loan_status: str | None,
) -> dict[str, Any]:
    """The answer, from Fineract's transaction. No Fineract ids."""
    return {
        "loan_reference": loan_reference,
        "client_uid": client_uid,
        "external_reference": external_reference,
        "type": "DISBURSEMENT",
        "status": _status_after(plan),
        "amount": whole_rwf(txn.get("amount")),
        "transaction_date": _date(txn.get("date")),
        "erp_status": plan.erp_status,
        "loan_status": loan_status,
    }


def repayment_answer(
    external_reference: str,
    loan_reference: str,
    client_uid: str | None,
    txn: Mapping[str, Any],
    plan: JournalPlan,
    loan_status: str | None,
    outstanding_after: int | None,
) -> dict[str, Any]:
    """The answer with Fineract's own split. No Fineract ids. outstanding_after is the
    principal still owed from Fineract's loan summary, read right after the repayment: the
    transaction's own running balance (outstandingLoanBalance) is rewritten by Fineract when
    it reprocesses same-day repayments, so it is not used."""
    body = disbursement_answer(
        external_reference, loan_reference, client_uid, txn, plan, loan_status
    )
    body["type"] = "REPAYMENT"
    loan_status_value = body.pop("loan_status")
    erp_status = body.pop("erp_status")
    body.update(
        {
            "principal_portion": whole_rwf(txn.get("principalPortion") or 0),
            "interest_portion": whole_rwf(txn.get("interestPortion") or 0),
            "overpayment_portion": whole_rwf(txn.get("overpaymentPortion") or 0),
            "fees_portion": whole_rwf(txn.get("feeChargesPortion") or 0),
            "penalties_portion": whole_rwf(txn.get("penaltyChargesPortion") or 0),
            "outstanding_after": outstanding_after,
            "erp_status": erp_status,
            "loan_status": loan_status_value,
        }
    )
    return body


def disbursement_rejection(
    error: FineractRejected, loan_reference: str, loan_status_id: int | None
) -> ApiError:
    """Fineract refused the disbursement. Its loan state is the last line of defence (L5)."""
    # Both codes mean "not now"; the loan's state says why. Unread state: already paid out,
    # the one case where sending again could pay twice.
    refused: tuple[ErrorCode, int, str] = (
        ErrorCode.LOAN_ALREADY_DISBURSED,
        409,
        f"Loan {loan_reference} is already paid out.",
    )
    if loan_status_id == NOT_APPROVED_STATUS_ID:
        refused = (ErrorCode.VALIDATION_ERROR, 422, f"Loan {loan_reference} is not approved yet.")
    elif loan_status_id == APPROVED_STATUS_ID:
        refused = (
            ErrorCode.VALIDATION_ERROR,
            422,
            f"Fineract refused to pay out loan {loan_reference} as approved.",
        )
    known = {
        DISBURSE_OVER_APPROVED: refused,
        DISBURSE_WRONG_STATE: refused,
        TXN_EXTERNAL_ID_TAKEN: _external_id_taken(),
    }
    return fineract_refusal(error, known, "disbursement")


def repayment_rejection(error: FineractRejected, loan_reference: str) -> ApiError:
    known = {
        REPAY_NOT_ACTIVE: (
            ErrorCode.VALIDATION_ERROR,
            422,
            f"Loan {loan_reference} is not paid out yet, so it takes no repayments.",
        ),
        TXN_EXTERNAL_ID_TAKEN: _external_id_taken(),
    }
    return fineract_refusal(error, known, "repayment")


def _external_id_taken() -> tuple[ErrorCode, int, str]:
    return (
        ErrorCode.IDEMPOTENCY_CONFLICT,
        409,
        "Fineract already holds another transaction under this external_reference.",
    )


# --- Fineract calls ---------------------------------------------------------------------------


def write_call(
    fineract: FineractApi,
    path: str,
    params: dict[str, Any],
    body: dict[str, Any],
    correlation_id: str | None,
    before: Callable[[], None] | None = None,
) -> Callable[[str], dict[str, Any]]:
    """The write for execute_fineract_write: call(idempotency_key). `before` runs first,
    inside the same attempt (a status check); it may raise to stop before anything is sent."""

    def call(key: str) -> dict[str, Any]:
        if before is not None:
            before()
        data = fineract.post(
            path, body, idempotency_key=key, params=params, correlation_id=correlation_id
        )
        return data if isinstance(data, dict) else {}

    return call


def transaction_lookup(
    fineract: FineractApi, loan_reference: str, op_key: str, correlation_id: str | None
) -> Callable[[], dict[str, Any] | None]:
    """Finds the loan transaction whose externalId is our op key. None when Fineract has
    none."""

    def lookup() -> dict[str, Any] | None:
        try:
            data = fineract.get(
                transaction_path(loan_reference, op_key), correlation_id=correlation_id
            )
        except FineractNotFound:
            return None
        return data if isinstance(data, dict) else None

    return lookup


# --- the flows --------------------------------------------------------------------------------


def disburse_loan(
    engine: Engine,
    fineract: FineractApi,
    loan_reference: str,
    request: DisbursementRequest,
    partner: str,
    correlation_id: str | None,
    accounts: Accounts,
    today: date | None = None,
) -> Answer:
    """Pays out the loan's full approved principal (from Fineract's template), once."""
    today = today or kigali_today()
    check_transaction_date(request.transaction_date, today)
    loan = _our_loan_or_404(engine, loan_reference)
    started = begin(
        engine,
        "DSB",
        request.external_reference,
        {"loan_reference": loan_reference, **request.model_dump(mode="json")},
        partner,
        correlation_id,
        client_uid=loan.client_uid,
        loan_reference=loan_reference,
    )
    if not isinstance(started, New):
        return answer_for_begin(started, request.external_reference)
    op = started.operation
    form = _template_or_fail(
        engine,
        op,
        lambda: fetch_disbursement_form(fineract, loan_reference, correlation_id),
        "disbursement",
        correlation_id,
    )
    try:
        on, amount = disbursement_terms(form, request.transaction_date, today)
    except ApiError as exc:
        fail(engine, op, exc, correlation_id)
    call = write_call(
        fineract,
        loan_path(loan_reference),
        {"command": "disburse"},
        build_disbursement_body(on, amount, op.op_key),
        correlation_id,
    )

    def refuse(error: FineractRejected) -> ApiError:
        return disbursement_rejection(
            error, loan_reference, _loan_status_id(fineract, loan_reference, correlation_id)
        )

    return _write_and_finish(engine, fineract, op, call, refuse, accounts, correlation_id)


def record_repayment(
    engine: Engine,
    fineract: FineractApi,
    loan_reference: str,
    request: RepaymentRequest,
    partner: str,
    correlation_id: str | None,
    accounts: Accounts,
    today: date | None = None,
) -> Answer:
    """Records one repayment. Fineract decides the split; we report it as it is."""
    today = today or kigali_today()
    on = check_transaction_date(request.transaction_date, today)
    loan = _our_loan_or_404(engine, loan_reference)
    started = begin(
        engine,
        "RPY",
        request.external_reference,
        {"loan_reference": loan_reference, **request.model_dump(mode="json")},
        partner,
        correlation_id,
        client_uid=loan.client_uid,
        loan_reference=loan_reference,
    )
    if not isinstance(started, New):
        return answer_for_begin(started, request.external_reference)
    op = started.operation
    form = _template_or_fail(
        engine,
        op,
        lambda: fetch_repayment_form(fineract, loan_reference, correlation_id),
        "repayment",
        correlation_id,
    )
    try:
        payment_type = repayment_payment_type(
            form, request.amount, request.transaction_date, request.payment_type, today
        )
    except ApiError as exc:
        fail(engine, op, exc, correlation_id)

    def still_active() -> None:
        try:
            data = fineract.get(loan_path(loan_reference), correlation_id=correlation_id)
        except FineractError as exc:
            # A read before the write: whatever went wrong, nothing was sent.
            raise FineractUnavailable("could not read the loan before the write") from exc
        check_repayable(data if isinstance(data, dict) else {}, loan_reference)

    call = write_call(
        fineract,
        f"{loan_path(loan_reference)}/transactions",
        {"command": "repayment"},
        build_repayment_body(on, request.amount, op.op_key, payment_type),
        correlation_id,
        before=still_active,
    )
    return _write_and_finish(
        engine,
        fineract,
        op,
        call,
        lambda error: repayment_rejection(error, loan_reference),
        accounts,
        correlation_id,
    )


def recover_money_operation(
    engine: Engine, fineract: FineractApi, op: Operation, accounts: Accounts
) -> str:
    """For recovery: a DSB or RPY left FINERACT_PENDING. Looks it up by its externalId and
    never sends it again. The caller holds the per-loan lock. Answers "done", "failed" or
    "unknown". The saved answer has the same shape the route gives."""
    if op.loan_reference is None:
        raise ValueError("a money operation always has a loan_reference")
    try:
        found = transaction_lookup(fineract, op.loan_reference, op.op_key, op.correlation_id)()
    except FineractError:
        return "unknown"
    if found is None:
        mark_failed(
            engine,
            op.id,
            "recovery: Fineract has no record of it, so nothing happened",
            op.correlation_id,
        )
        return "failed"
    _finish(engine, fineract, op, found, accounts, op.correlation_id, "recovery: found in Fineract")
    return "done"


# --- shared steps -----------------------------------------------------------------------------


def _our_loan_or_404(engine: Engine, loan_reference: str) -> OurLoan:
    loan = our_loan(engine, loan_reference)
    if loan is None:
        raise ApiError(ErrorCode.LOAN_NOT_FOUND, f"No loan {loan_reference}.", 404)
    return loan


def _template_or_fail[T](
    engine: Engine,
    op: Operation,
    read: Callable[[], T],
    what: str,
    correlation_id: str | None,
) -> T:
    """Fineract's template, read before the write. Whatever went wrong (no answer, an error,
    an answer we cannot read), nothing was sent: FAILED and 503, so the partner sends the
    same request again."""
    try:
        return read()
    except FineractError as exc:
        log_event(
            f"could not read the {what} template from Fineract",
            level=logging.WARNING,
            correlation_id=correlation_id,
            external_reference=op.external_reference,
            loan_reference=op.loan_reference,
            problem=type(exc).__name__,
        )
        fail(
            engine,
            op,
            ApiError(
                ErrorCode.FINERACT_UNAVAILABLE,
                f"Could not read Fineract's {what} template. Nothing was sent. Send the "
                "same request again.",
                503,
            ),
            correlation_id,
        )


def _write_and_finish(
    engine: Engine,
    fineract: FineractApi,
    op: Operation,
    call: Callable[[str], dict[str, Any]],
    refuse: Callable[[FineractRejected], ApiError],
    accounts: Accounts,
    correlation_id: str | None,
) -> Answer:
    """The write under the per-loan lock (L3), then Fineract's own transaction, then
    FINERACT_DONE + saved answer + outbox row in one transaction. All of it inside the lock,
    so the loan status in the answer is the one right after this transaction."""
    assert op.loan_reference is not None  # DSB and RPY rows always carry one (CHECK)
    lookup = transaction_lookup(fineract, op.loan_reference, op.op_key, correlation_id)
    try:
        with loan_lock(engine, op.loan_reference):
            try:
                outcome = execute_fineract_write(
                    call, lookup, op.op_key, op.attempts, check_first=op.attempts > 1
                )
            except LoanNotRepayable as exc:
                outcome = _ours_after_all(lookup, op, ()) or fail(
                    engine, op, exc.error, correlation_id
                )
            settled = settle(engine, op, outcome, correlation_id)
            if isinstance(settled, Rejected):
                rechecked = _ours_after_all(lookup, op, settled.error.error_codes)
                if rechecked is None:
                    fail(engine, op, refuse(settled.error), correlation_id)
                settled = settle(engine, op, rechecked, correlation_id)
                assert isinstance(settled, Done)
            txn = settled.result if settled.found_by_lookup else _read_back(lookup)
            if txn is None:
                return _still_finishing(engine, op, correlation_id)
            return _finish(engine, fineract, op, txn, accounts, correlation_id, "Fineract said yes")
    except LoanLockTimeoutError:
        fail(
            engine,
            op,
            ApiError(
                ErrorCode.FINERACT_UNAVAILABLE,
                f"Another write to loan {op.loan_reference} is still running. Nothing was "
                "sent. Send the same request again.",
                503,
            ),
            correlation_id,
        )


def _ours_after_all(
    lookup: Callable[[], dict[str, Any] | None], op: Operation, refusal_codes: Iterable[str]
) -> Done[dict[str, Any]] | Unknown | None:
    """Fineract refused this attempt (or the loan no longer looks repayable). On a retry, or
    when Fineract says our externalId is taken, the cause can be our own earlier attempt
    that landed late. The transaction under our op key is ours, so look it up before
    calling it a refusal. None: not there, it really is a refusal."""
    if op.attempts <= 1 and TXN_EXTERNAL_ID_TAKEN not in refusal_codes:
        return None
    try:
        found = lookup()
    except FineractError:
        return Unknown("refused, and the lookup for an earlier attempt failed")
    return Done(found, found_by_lookup=True) if found is not None else None


def _read_back(lookup: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
    """Fineract said yes: read its transaction by our externalId. None if it cannot be read
    now (recovery will)."""
    try:
        return lookup()
    except FineractError:
        return None


def _still_finishing(engine: Engine, op: Operation, correlation_id: str | None) -> Answer:
    """Fineract took it, but its transaction could not be read back yet. The operation stays
    FINERACT_PENDING and recovery finishes it at its next pass; until then a repeat gets 202."""
    reason = "Fineract took it; its transaction could not be read back yet"
    set_fields(engine, op.id, last_error=reason, next_attempt_at=datetime.now(UTC))
    log_event(
        reason,
        level=logging.WARNING,
        correlation_id=correlation_id,
        external_reference=op.external_reference,
        client_uid=op.client_uid,
        loan_reference=op.loan_reference,
        status=FINERACT_PENDING,
    )
    return answer_for_begin(
        InProgress(get_operation(engine, op.op_type, op.external_reference)), op.external_reference
    )


def _finish(
    engine: Engine,
    fineract: FineractApi,
    op: Operation,
    txn: dict[str, Any],
    accounts: Accounts,
    correlation_id: str | None,
    detail: str,
) -> Answer:
    assert op.loan_reference is not None
    plan = money_plan(op.op_type, op.op_key, op.loan_reference, txn, accounts)
    status, principal_left = _loan_after(fineract, op.loan_reference, correlation_id)
    if op.op_type == "DSB":
        body = disbursement_answer(
            op.external_reference, op.loan_reference, op.client_uid, txn, plan, status
        )
    else:
        body = repayment_answer(
            op.external_reference,
            op.loan_reference,
            op.client_uid,
            txn,
            plan,
            status,
            principal_left,
        )
    transaction_id = _as_int(txn.get("id"))
    with engine.begin() as conn:
        record_money_done(
            conn,
            op.id,
            plan,
            response_status=200,
            response_body=body,
            fineract_resource_id=_as_int(txn.get("loanId")),
            fineract_transaction_id=transaction_id,
            detail=detail,
            correlation_id=correlation_id,
        )
    log_event(
        f"{TYPE_NAMES[op.op_type].lower()} done in Fineract; journal entry {plan.erp_status}",
        level=logging.INFO if plan.entry is not None else logging.WARNING,
        correlation_id=correlation_id,
        external_reference=op.external_reference,
        client_uid=op.client_uid,
        loan_reference=op.loan_reference,
        fineract_transaction_id=transaction_id,
        erp_transaction_id=None,
        status=body["status"],
        problem=plan.problem,
    )
    return Answer(200, body)


def _loan_after(
    fineract: FineractApi, loan_reference: str, correlation_id: str | None
) -> tuple[str | None, int | None]:
    """The loan as Fineract shows it now: its status in our form (e.g. ACTIVE) and the
    principal still owed from its summary. Best effort: None for what cannot be read."""
    try:
        data = fineract.get(loan_path(loan_reference), correlation_id=correlation_id)
    except FineractError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    status = data.get("status")
    code = loan_status_code(status.get("code")) if isinstance(status, dict) else None
    summary = data.get("summary")
    left = whole_rwf(summary.get("principalOutstanding")) if isinstance(summary, dict) else None
    return code, left


def _loan_status_id(
    fineract: FineractApi, loan_reference: str, correlation_id: str | None
) -> int | None:
    try:
        loan = find_loan(fineract, loan_reference, correlation_id)
    except FineractError:
        return None
    return loan.status_id if loan else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, int | float) else None
