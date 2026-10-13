"""Loans: application, approval and the loan as Fineract reports it.

Every write follows the same steps (later loan writes, disbursement and repayment, reuse the
helpers in the first section):
1. Check what we can without Fineract (dates, our own records). A 4xx here saves nothing.
2. begin() in services.operations. Only New runs; a repeat gets its saved answer, 202,
   or 409 (answer_for_begin).
3. A retry (attempt 2 or later) first looks up its earlier attempt by our key: one that
   landed late is ours and is finished, and a failed lookup leaves it unknown (504,
   FINERACT_PENDING). Only then:
   Read what the write needs from Fineract. If that fails, nothing was sent: FAILED.
   The rules and terms come from Fineract's own templates (services.templates): the
   product's principal range and terms for an application, the approved amount and default
   date for an approval. A value that does not fit is 422 VALIDATION_ERROR with
   details.problems, and FAILED.
4. The write itself, under the per-loan advisory lock, through execute_fineract_write.
   It is looked up before sending when an earlier attempt exists (check_first).
5. settle(): not sent -> FAILED and 503; unclear (including "not found yet" right after a
   timeout, 5xx or 425) -> stays FINERACT_PENDING and 504 (never sent again blindly; a
   repeat gets 202 until recovery has looked it up once the request is stale); refused -> FAILED
   and a partner error in our own words; done -> our record and the saved answer in one
   transaction (mark_fineract_done).

Loan references: issued from loan_reference_seq at the first execution and stored on the
operation, so a retry of a FAILED request and every replay use the same one. Fineract can
already hold a reference we issue (when integration_db was restored or rebuilt and the
sequence started again): then the request gets a new reference, up to MAX_REFERENCE_TRIES.

Fineract's numeric ids are kept in our tables for our own use. No answer here shows one."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, NoReturn, Protocol

from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from integration_service.clients.errors import (
    FineractError,
    FineractNotFound,
    FineractRejected,
    FineractServerError,
    FineractTimeout,
    FineractUnavailable,
)
from integration_service.clients.fineract import FineractClient
from integration_service.config import Settings
from integration_service.errors import ApiError, ErrorCode
from integration_service.logs import log_event
from integration_service.models import clients, loans
from integration_service.schemas.loans import ApprovalRequest, LoanApplicationRequest
from integration_service.services.fineract_calls import (
    Done,
    NotSent,
    Outcome,
    Rejected,
    Unknown,
    execute_fineract_write,
    look_up_earlier,
)
from integration_service.services.loan_locks import LoanLockTimeoutError, loan_lock
from integration_service.services.operations import (
    TYPE_NAMES,
    Conflict,
    InProgress,
    New,
    Operation,
    Replay,
    Taken,
    begin,
    get_operation,
    mark_failed,
    mark_fineract_done,
    next_loan_reference,
    set_fields,
)
from integration_service.services.templates import (
    FINERACT_DATE_FORMAT,
    FINERACT_LOCALE,
    ApprovalForm,
    FieldProblem,
    LoanApplicationForm,
    build_application_body,
    check_application,
    check_approval,
    fetch_approval_form,
    fetch_loan_application_form,
    fetch_payoff_form,
    validation_error,
)
from integration_service.state_machine import FAILED, FINERACT_PENDING, NEEDS_RECONCILIATION

# Fineract's tenant runs on Kigali time (UTC+2, no daylight saving). "Today" for every date
# we send is the Kigali date, or Fineract refuses it as a future date.
KIGALI = timezone(timedelta(hours=2), "Africa/Kigali")
MAX_REFERENCE_TRIES = 3

SUBMITTED = "SUBMITTED_AND_PENDING_APPROVAL"
APPROVED = "APPROVED"
ACTIVE = "ACTIVE"

LOAN_EXTERNAL_ID_TAKEN = "error.msg.loan.with.externalId.already.used"
NOT_AUTHORIZED = "error.msg.not.authorized"
# Loan statuses (Fineract ids) that come after approval.
_APPROVED_OR_LATER = frozenset({200, 300, 600, 601, 602, 700})


class FineractApi(Protocol):
    """The part of FineractClient these services use. Tests can pass a stand-in."""

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any: ...

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any: ...


def fineract_from_settings(settings: Settings) -> FineractClient:
    return FineractClient(
        base_url=settings.fineract_base_url,
        tenant=settings.fineract_tenant,
        username=settings.fineract_username,
        password=settings.fineract_password,
        timeout_seconds=settings.fineract_timeout_seconds,
    )


# --- shared by every partner write ------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """What the route sends back: HTTP status and JSON body."""

    status: int
    body: dict[str, Any]


_TAKEN_MESSAGES = {
    ErrorCode.CLIENT_UID_TAKEN: "This client_uid is already used by another external_reference.",
    ErrorCode.LOAN_ALREADY_APPROVED: "This loan is already approved, or being approved, "
    "under another external_reference.",
    ErrorCode.LOAN_ALREADY_DISBURSED: "This loan is already paid out, or being paid out, "
    "under another external_reference.",
}


def answer_for_begin(
    result: Replay | InProgress | Conflict | Taken, external_reference: str
) -> Answer:
    """The answer for a request that must not run now. Raises ApiError for a 409.
    A finished request answers 200 with its saved body, even when the first answer was 201."""
    if isinstance(result, Replay):
        return Answer(200, result.body)
    if isinstance(result, InProgress):
        op = result.operation
        if op is not None and op.status == NEEDS_RECONCILIATION:
            # Nothing is running and nothing will run again: it waits for a manual repair.
            raise ApiError(
                ErrorCode.NEEDS_RECONCILIATION,
                "We could not confirm with Fineract whether this request worked, so it "
                "waits for a manual check by our staff. It will not run again. See GET "
                f"/api/transactions/{external_reference}.",
                409,
                details={"status": op.status, "type": TYPE_NAMES[op.op_type]},
            )
        return Answer(
            202,
            {
                "external_reference": external_reference,
                "type": TYPE_NAMES[op.op_type] if op else None,
                "status": op.status if op else None,
                "client_uid": op.client_uid if op else None,
                "loan_reference": op.loan_reference if op else None,
                "message": "This request is still running and will not run twice. "
                f"See GET /api/transactions/{external_reference}.",
            },
        )
    if isinstance(result, Conflict):
        raise ApiError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            f"external_reference {external_reference} was already used with different data.",
            409,
        )
    raise ApiError(
        result.code,
        _TAKEN_MESSAGES.get(result.code, "Already taken by another external_reference."),
        409,
        details={"holder_status": result.holder_status},
    )


def fineract_refusal(
    error: FineractRejected, known: Mapping[str, tuple[ErrorCode, int, str]], what: str
) -> ApiError:
    """A clear partner error for a Fineract 4xx. Uses Fineract's error codes only: its
    messages can hold Fineract ids."""
    if error.status == 401 or NOT_AUTHORIZED in error.error_codes:
        return ApiError(
            ErrorCode.FINERACT_UNAVAILABLE,
            "Fineract refused the Integration Service's own login or permissions. "
            "This is on our side.",
            502,
        )
    for code in error.error_codes:
        if code in known:
            partner_code, status, message = known[code]
            return ApiError(partner_code, message, status)
    return ApiError(
        ErrorCode.VALIDATION_ERROR,
        f"Fineract refused the {what}.",
        422,
        details={"fineract_codes": list(error.error_codes)},
    )


def fineract_read_error(exc: FineractError) -> ApiError:
    """A read we needed failed. Our own words only."""
    if isinstance(exc, FineractUnavailable):
        return ApiError(ErrorCode.FINERACT_UNAVAILABLE, "Fineract is not reachable.", 503)
    if isinstance(exc, FineractTimeout):
        return ApiError(ErrorCode.FINERACT_TIMEOUT, "Fineract did not answer in time.", 504)
    if isinstance(exc, FineractRejected):
        return ApiError(
            ErrorCode.FINERACT_UNAVAILABLE,
            f"Fineract refused a read we needed (HTTP {exc.status}).",
            502,
        )
    return ApiError(ErrorCode.FINERACT_UNAVAILABLE, "Fineract answered with an error.", 502)


def fail(engine: Engine, op: Operation, error: ApiError, correlation_id: str | None) -> NoReturn:
    """We know nothing happened in Fineract: FAILED with our message, then the error."""
    mark_failed(engine, op.id, error.message, correlation_id)
    raise error


def read_or_fail[T](
    engine: Engine, op: Operation, read: Callable[[], T], what: str, correlation_id: str | None
) -> T:
    """A Fineract read before the write. If it fails, nothing was sent: FAILED."""
    try:
        return read()
    except FineractError as exc:
        error = fineract_read_error(exc)
        mark_failed(
            engine, op.id, f"could not read {what} from Fineract; nothing was sent", correlation_id
        )
        raise error from exc


def earlier_attempt[T](
    engine: Engine, op: Operation, lookup: Callable[[], T | None], correlation_id: str | None
) -> T | None:
    """A retry looks for its earlier attempt before anything else is read or checked: one
    that landed late is ours (the result). None: the first attempt, or nothing found. A
    failed lookup leaves the outcome unknown: 504 and FINERACT_PENDING (settle)."""
    found = look_up_earlier(lookup, op.attempts)
    if found is None:
        return None
    settled = settle(engine, op, found, correlation_id)
    assert isinstance(settled, Done)
    return settled.result


def write_to_loan[T](
    engine: Engine,
    op: Operation,
    loan_reference: str,
    attempt: int,
    call: Callable[[str], T],
    lookup: Callable[[], T | None],
    correlation_id: str | None,
) -> Outcome[T]:
    """One Fineract write to one loan, under the per-loan lock (L3). An attempt after the
    first is looked up before sending."""
    try:
        with loan_lock(engine, loan_reference):
            return execute_fineract_write(call, lookup, op.op_key, attempt, check_first=attempt > 1)
    except LoanLockTimeoutError:
        fail(
            engine,
            op,
            ApiError(
                ErrorCode.FINERACT_UNAVAILABLE,
                f"Another write to loan {loan_reference} is still running. Nothing was sent. "
                "Send the same request again.",
                503,
            ),
            correlation_id,
        )


def settle[T](
    engine: Engine, op: Operation, outcome: Outcome[T], correlation_id: str | None
) -> Done[T] | Rejected:
    """Not sent -> FAILED and 503. Unclear -> stays FINERACT_PENDING and 504. Otherwise the
    caller handles Done or Rejected."""
    if isinstance(outcome, NotSent):
        fail(
            engine,
            op,
            ApiError(
                ErrorCode.FINERACT_UNAVAILABLE,
                "Fineract did not take the request, so nothing was done. "
                "Send the same request again.",
                503,
                details={"external_reference": op.external_reference},
            ),
            correlation_id,
        )
    if isinstance(outcome, Unknown):
        set_fields(engine, op.id, last_error=outcome.reason)
        raise ApiError(
            ErrorCode.FINERACT_TIMEOUT,
            "We could not confirm with Fineract whether this worked. It will not be sent "
            "again until we know. See GET /api/transactions/"
            f"{op.external_reference}.",
            504,
            details={"external_reference": op.external_reference, "status": FINERACT_PENDING},
        )
    return outcome


# --- pure rules -------------------------------------------------------------------------------


def kigali_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(KIGALI).date()


def whole_rwf(value: Any) -> int | None:
    """Fineract sends amounts as floats (82000.0) or ints. RWF has no cents."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return int(amount)


def fineract_date(value: Any) -> date | None:
    """Fineract sends dates as [year, month, day]."""
    if isinstance(value, list) and len(value) == 3 and all(isinstance(v, int) for v in value):
        return date(value[0], value[1], value[2])
    return None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def status_name(code: Any, prefix: str) -> str:
    """'loanStatusType.closed.obligations.met' -> 'CLOSED_OBLIGATIONS_MET'."""
    if not isinstance(code, str) or not code:
        return "UNKNOWN"
    return code.removeprefix(prefix).replace(".", "_").upper()


def loan_status_code(code: Any) -> str:
    return status_name(code, "loanStatusType.")


def _id_of(value: Any) -> int:
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("expected an id")
    return value


def build_approval_body(approved_on: date, approved_amount: int) -> dict[str, Any]:
    return {
        "approvedOnDate": approved_on.isoformat(),
        "approvedLoanAmount": approved_amount,
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": FINERACT_LOCALE,
    }


def past_expected_date_error() -> ApiError:
    return validation_error(
        [FieldProblem("expected_disbursement_date", "cannot be in the past (Kigali date)")]
    )


def application_error(
    form: LoanApplicationForm, principal: int, expected: date, today: date
) -> ApiError | None:
    """The application checked against the product's template, or None when it fits."""
    problems = check_application(form, principal, expected, today)
    return validation_error(problems) if problems else None


@dataclass(frozen=True)
class ApprovalTerms:
    approved_on: date
    amount: int


def approval_terms(form: ApprovalForm, requested_on: date | None, today: date) -> ApprovalTerms:
    """What the approval sends: the partner's date, else Fineract's proposal (else today), and
    the amount Fineract's approval template proposes. Raises 422 when a value does not fit."""
    approved_on = requested_on or form.approved_on or today
    problems = check_approval(form, approved_on, today)
    if problems:
        raise validation_error(problems)
    return ApprovalTerms(approved_on, form.approved_amount)


def loan_view(
    data: Mapping[str, Any], loan_reference: str, client_uid: str, payoff_amount: int | None
) -> dict[str, Any]:
    """Fineract's loan in our names. Every Fineract id is left out."""
    status = data.get("status") or {}
    timeline = data.get("timeline") or {}
    currency = data.get("currency") or {}
    summary = data.get("summary")
    balances = None
    if isinstance(summary, dict):
        balances = {
            "principal_disbursed": whole_rwf(summary.get("principalDisbursed")),
            "principal_repaid": whole_rwf(summary.get("principalPaid")),
            "principal_outstanding": whole_rwf(summary.get("principalOutstanding")),
            "interest_charged": whole_rwf(summary.get("interestCharged")),
            "interest_repaid": whole_rwf(summary.get("interestPaid")),
            "interest_outstanding": whole_rwf(summary.get("interestOutstanding")),
            "fees_outstanding": whole_rwf(summary.get("feeChargesOutstanding")),
            "penalties_outstanding": whole_rwf(summary.get("penaltyChargesOutstanding")),
            "total_expected_repayment": whole_rwf(summary.get("totalExpectedRepayment")),
            "total_repaid": whole_rwf(summary.get("totalRepayment")),
            "total_outstanding": whole_rwf(summary.get("totalOutstanding")),
            "total_overdue": whole_rwf(summary.get("totalOverdue")),
            "overpaid_amount": whole_rwf(data.get("totalOverpaid", 0)),
        }
    return {
        "loan_reference": loan_reference,
        "client_uid": client_uid,
        "status": {"code": loan_status_code(status.get("code")), "value": status.get("value", "")},
        "closed_obligations_met": bool(status.get("closedObligationsMet")),
        "overpaid": bool(status.get("overpaid")),
        "currency": currency.get("code"),
        "principal": whole_rwf(data.get("principal")),
        "approved_principal": whole_rwf(data.get("approvedPrincipal")),
        "submitted_on": _iso(fineract_date(timeline.get("submittedOnDate"))),
        "approved_on": _iso(fineract_date(timeline.get("approvedOnDate"))),
        "expected_disbursement_on": _iso(fineract_date(timeline.get("expectedDisbursementDate"))),
        "disbursed_on": _iso(fineract_date(timeline.get("actualDisbursementDate"))),
        "closed_on": _iso(fineract_date(timeline.get("closedOnDate"))),
        "expected_maturity_on": _iso(fineract_date(timeline.get("expectedMaturityDate"))),
        "balances": balances,
        "payoff_amount": payoff_amount,
    }


def application_rejection(error: FineractRejected, client_uid: str) -> ApiError:
    known = {
        "error.msg.client.not.active.exception": (
            ErrorCode.VALIDATION_ERROR,
            422,
            f"Client {client_uid} is not active in Fineract.",
        ),
    }
    return fineract_refusal(error, known, "loan application")


def approval_rejection(error: FineractRejected, loan_reference: str) -> ApiError:
    known = {
        "error.msg.loan.approve.account.is.not.submitted.and.pending.state": (
            ErrorCode.LOAN_ALREADY_APPROVED,
            409,
            f"Loan {loan_reference} is already approved.",
        ),
    }
    return fineract_refusal(error, known, "approval")


def application_body(
    loan_reference: str,
    client_uid: str,
    principal: int,
    expected_disbursement_on: date,
    external_reference: str,
) -> dict[str, Any]:
    return {
        "loan_reference": loan_reference,
        "client_uid": client_uid,
        "principal": principal,
        "status": SUBMITTED,
        "expected_disbursement_date": expected_disbursement_on.isoformat(),
        "external_reference": external_reference,
    }


def approval_body(
    loan_reference: str,
    client_uid: str,
    approved_on: date,
    approved_principal: int,
    external_reference: str,
) -> dict[str, Any]:
    return {
        "loan_reference": loan_reference,
        "client_uid": client_uid,
        "status": APPROVED,
        "approved_on": approved_on.isoformat(),
        "approved_principal": approved_principal,
        "external_reference": external_reference,
    }


# --- our records ------------------------------------------------------------------------------


@dataclass(frozen=True)
class OurLoan:
    loan_reference: str
    client_uid: str
    principal: int
    status: str
    fineract_loan_id: int | None


def our_loan(engine: Engine, loan_reference: str) -> OurLoan | None:
    with engine.connect() as conn:
        row = (
            conn.execute(select(loans).where(loans.c.loan_reference == loan_reference))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    return OurLoan(
        loan_reference=row["loan_reference"],
        client_uid=row["client_uid"],
        principal=row["principal"],
        status=row["status"],
        fineract_loan_id=row["fineract_loan_id"],
    )


def _fineract_client_id(engine: Engine, client_uid: str) -> int | None:
    with engine.connect() as conn:
        value: int | None = conn.execute(
            select(clients.c.fineract_client_id).where(clients.c.client_uid == client_uid)
        ).scalar_one_or_none()
    return value


def _issue_loan_reference(engine: Engine, op_id: int, **values: Any) -> str:
    with engine.begin() as conn:
        reference = next_loan_reference(conn)
        set_fields(conn, op_id, loan_reference=reference, **values)
    return reference


# --- Fineract reads and writes ----------------------------------------------------------------


@dataclass(frozen=True)
class FineractLoan:
    """What a write or a lookup tells us about one loan. fineract_id is internal."""

    fineract_id: int
    client_uid: str | None
    principal: int | None
    expected_disbursement_on: date | None
    approved_on: date | None
    approved_principal: int | None
    status_id: int | None


def parse_loan(data: Mapping[str, Any]) -> FineractLoan:
    status = data.get("status") or {}
    timeline = data.get("timeline") or {}
    try:
        fineract_id = _id_of(data.get("id"))
    except ValueError as exc:
        raise FineractServerError(200, dict(data), "Unexpected Fineract loan") from exc
    status_id = status.get("id")
    return FineractLoan(
        fineract_id=fineract_id,
        client_uid=data.get("clientExternalId"),
        principal=whole_rwf(data.get("principal")),
        expected_disbursement_on=fineract_date(timeline.get("expectedDisbursementDate")),
        approved_on=fineract_date(timeline.get("approvedOnDate")),
        approved_principal=whole_rwf(data.get("approvedPrincipal")),
        status_id=status_id if isinstance(status_id, int) else None,
    )


def get_fineract_loan(
    fineract: FineractApi, loan_reference: str, correlation_id: str | None
) -> dict[str, Any] | None:
    """The loan by its externalId, or None when Fineract does not know it."""
    try:
        data = fineract.get(f"/loans/external-id/{loan_reference}", correlation_id=correlation_id)
    except FineractNotFound:
        return None
    return data if isinstance(data, dict) else None


def find_loan(
    fineract: FineractApi, loan_reference: str, correlation_id: str | None
) -> FineractLoan | None:
    data = get_fineract_loan(fineract, loan_reference, correlation_id)
    return parse_loan(data) if data is not None else None


def _resource_id(data: Any, *names: str) -> int:
    if isinstance(data, dict):
        for name in names:
            value = data.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    body = data if isinstance(data, dict) else {}
    raise FineractServerError(200, body, "Unexpected Fineract answer (no resource id)")


# --- loan application -------------------------------------------------------------------------


def apply_for_loan(
    engine: Engine,
    fineract: FineractApi,
    request: LoanApplicationRequest,
    partner: str,
    correlation_id: str | None,
    today: date | None = None,
) -> Answer:
    today = today or kigali_today()
    reference = request.external_reference
    expected = request.expected_disbursement_date or today
    past = past_expected_date_error()
    # A date that has passed since the first request must not hide its saved answer (Part F):
    # refuse it here only when this reference has nothing that begin() would answer with.
    if expected < today:
        saved = get_operation(engine, "APP", reference)
        if saved is None or saved.status == FAILED:
            raise past
    client_id = _fineract_client_id(engine, request.client_uid)
    if client_id is None:
        raise ApiError(
            ErrorCode.CLIENT_NOT_FOUND,
            f"No client {request.client_uid}. Create it with POST /api/clients first.",
            404,
        )

    started = begin(
        engine,
        "APP",
        reference,
        request.model_dump(mode="json"),
        partner,
        correlation_id,
        client_uid=request.client_uid,
    )
    if not isinstance(started, New):
        return answer_for_begin(started, reference)
    op = started.operation
    if op.loan_reference is not None:
        earlier_ref = op.loan_reference
        held = earlier_attempt(
            engine, op, lambda: find_loan(fineract, earlier_ref, correlation_id), correlation_id
        )
        if held is not None and _is_ours(held, request.client_uid, request.principal):
            return _finish_application(
                engine,
                op,
                earlier_ref,
                request.client_uid,
                request.principal,
                held.expected_disbursement_on or expected,
                held.fineract_id,
                correlation_id,
            )
    if expected < today:  # it FAILED meanwhile and is retried now: the date rule applies
        fail(engine, op, past, correlation_id)

    form = read_or_fail(
        engine,
        op,
        lambda: fetch_loan_application_form(fineract, client_id, correlation_id=correlation_id),
        "the loan application template",
        correlation_id,
    )
    refusal = application_error(form, request.principal, expected, today)
    if refusal is not None:
        fail(engine, op, refusal, correlation_id)

    loan_reference = op.loan_reference or _issue_loan_reference(engine, op.id)
    attempt = op.attempts
    for _ in range(MAX_REFERENCE_TRIES):
        body = build_application_body(
            form, client_id, request.principal, loan_reference, today, expected
        )
        ref = loan_reference

        def submit(key: str, body: dict[str, Any] = body) -> FineractLoan:
            data = fineract.post("/loans", body, idempotency_key=key, correlation_id=correlation_id)
            return FineractLoan(
                fineract_id=_resource_id(data, "loanId", "resourceId"),
                client_uid=request.client_uid,
                principal=request.principal,
                expected_disbursement_on=expected,
                approved_on=None,
                approved_principal=None,
                status_id=100,
            )

        def lookup(ref: str = ref) -> FineractLoan | None:
            return find_loan(fineract, ref, correlation_id)

        outcome = write_to_loan(engine, op, ref, attempt, submit, lookup, correlation_id)
        settled = settle(engine, op, outcome, correlation_id)
        if isinstance(settled, Done):
            loan = settled.result
            if not settled.found_by_lookup or _is_ours(loan, request.client_uid, request.principal):
                return _finish_application(
                    engine,
                    op,
                    ref,
                    request.client_uid,
                    request.principal,
                    expected,
                    loan.fineract_id,
                    correlation_id,
                )
        elif LOAN_EXTERNAL_ID_TAKEN in settled.error.error_codes:
            # An earlier attempt of this request may have landed late; else it is not ours.
            held = _lookup_after_taken(engine, op, fineract, ref, correlation_id)
            if attempt > 1 and held and _is_ours(held, request.client_uid, request.principal):
                return _finish_application(
                    engine,
                    op,
                    ref,
                    request.client_uid,
                    request.principal,
                    expected,
                    held.fineract_id,
                    correlation_id,
                )
        else:
            fail(
                engine, op, application_rejection(settled.error, request.client_uid), correlation_id
            )

        # Fineract already holds another loan under this reference: issue a new one.
        attempt += 1
        loan_reference = _issue_loan_reference(engine, op.id, attempts=attempt)
        log_event(
            "loan_reference already used in Fineract; issued a new one",
            correlation_id=correlation_id,
            external_reference=reference,
            client_uid=request.client_uid,
            loan_reference=loan_reference,
            previous_loan_reference=ref,
        )
    fail(
        engine,
        op,
        ApiError(ErrorCode.INTERNAL_ERROR, "Could not issue a free loan_reference.", 500),
        correlation_id,
    )


def _is_ours(loan: FineractLoan, client_uid: str, principal: int) -> bool:
    return loan.client_uid == client_uid and loan.principal == principal


def _lookup_after_taken(
    engine: Engine, op: Operation, fineract: FineractApi, ref: str, correlation_id: str | None
) -> FineractLoan | None:
    try:
        return find_loan(fineract, ref, correlation_id)
    except FineractError:
        set_fields(engine, op.id, last_error="lookup failed after a duplicate loan_reference")
        raise ApiError(
            ErrorCode.FINERACT_TIMEOUT,
            "We could not confirm with Fineract whether this worked. "
            f"See GET /api/transactions/{op.external_reference}.",
            504,
            details={"external_reference": op.external_reference, "status": FINERACT_PENDING},
        ) from None


def _finish_application(
    engine: Engine,
    op: Operation,
    loan_reference: str,
    client_uid: str,
    principal: int,
    expected: date,
    fineract_loan_id: int,
    correlation_id: str | None,
) -> Answer:
    body = application_body(loan_reference, client_uid, principal, expected, op.external_reference)
    with engine.begin() as conn:
        conn.execute(
            pg_insert(loans)
            .values(
                loan_reference=loan_reference,
                client_uid=client_uid,
                external_reference=op.external_reference,
                fineract_loan_id=fineract_loan_id,
                principal=principal,
                status=SUBMITTED,
            )
            .on_conflict_do_nothing(index_elements=["loan_reference"])
        )
        mark_fineract_done(
            conn,
            op.id,
            response_status=201,
            response_body=body,
            fineract_resource_id=fineract_loan_id,
            detail="loan application saved in Fineract",
            correlation_id=correlation_id,
        )
    return Answer(201, body)


def recover_application(
    engine: Engine, fineract: FineractApi, op: Operation, correlation_id: str | None = None
) -> str:
    """For recovery: an APP left FINERACT_PENDING. Looks it up; never sends it again.
    Answers what it did: "done", "failed" or "unknown"."""
    if op.loan_reference is None or op.client_uid is None:
        mark_failed(engine, op.id, "no loan_reference was issued; nothing was sent", correlation_id)
        return "failed"
    try:
        loan = find_loan(fineract, op.loan_reference, correlation_id)
    except FineractError:
        return "unknown"
    if loan is None or loan.client_uid != op.client_uid or loan.principal is None:
        mark_failed(engine, op.id, "recovery lookup found nothing of ours", correlation_id)
        return "failed"
    _finish_application(
        engine,
        op,
        op.loan_reference,
        op.client_uid,
        loan.principal,
        loan.expected_disbursement_on or op.created_at.astimezone(KIGALI).date(),
        loan.fineract_id,
        correlation_id,
    )
    return "done"


# --- approval ---------------------------------------------------------------------------------


def approve_loan(
    engine: Engine,
    fineract: FineractApi,
    loan_reference: str,
    request: ApprovalRequest,
    partner: str,
    correlation_id: str | None,
    today: date | None = None,
) -> Answer:
    today = today or kigali_today()
    # A future date is refused before anything is saved; the template check below repeats it.
    if request.approved_on is not None and request.approved_on > today:
        raise validation_error(
            [FieldProblem("approved_on", "cannot be in the future (Kigali date)")]
        )
    loan = our_loan(engine, loan_reference)
    if loan is None:
        raise ApiError(ErrorCode.LOAN_NOT_FOUND, f"No loan {loan_reference}.", 404)

    reference = request.external_reference
    started = begin(
        engine,
        "APR",
        reference,
        {"loan_reference": loan_reference, **request.model_dump(mode="json")},
        partner,
        correlation_id,
        client_uid=loan.client_uid,
        loan_reference=loan_reference,
    )
    if not isinstance(started, New):
        return answer_for_begin(started, reference)
    op = started.operation
    earlier = earlier_attempt(
        engine, op, lambda: find_approval(fineract, loan_reference, correlation_id), correlation_id
    )
    if earlier is not None:
        return _finish_approval(engine, op, loan.client_uid, earlier, correlation_id)
    form = read_or_fail(
        engine,
        op,
        lambda: read_approval_form(fineract, loan_reference, correlation_id),
        "the approval template",
        correlation_id,
    )
    try:
        terms = approval_terms(form, request.approved_on, today)
    except ApiError as refusal:
        fail(engine, op, refusal, correlation_id)
    body = build_approval_body(terms.approved_on, terms.amount)

    def approve(key: str) -> FineractLoan:
        data = fineract.post(
            f"/loans/external-id/{loan_reference}",
            body,
            idempotency_key=key,
            params={"command": "approve"},
            correlation_id=correlation_id,
        )
        return FineractLoan(
            fineract_id=_resource_id(data, "loanId", "resourceId"),
            client_uid=loan.client_uid,
            principal=loan.principal,
            expected_disbursement_on=None,
            approved_on=terms.approved_on,
            approved_principal=terms.amount,
            status_id=200,
        )

    outcome = write_to_loan(
        engine,
        op,
        loan_reference,
        op.attempts,
        approve,
        lambda: find_approval(fineract, loan_reference, correlation_id),
        correlation_id,
    )
    settled = settle(engine, op, outcome, correlation_id)
    if isinstance(settled, Rejected):
        fail(engine, op, approval_rejection(settled.error, loan_reference), correlation_id)
    return _finish_approval(engine, op, loan.client_uid, settled.result, correlation_id)


def read_approval_form(
    fineract: FineractApi, loan_reference: str, correlation_id: str | None
) -> ApprovalForm:
    """Fineract's approval template. One without an amount to approve is unexpected."""
    form = fetch_approval_form(fineract, loan_reference, correlation_id)
    if form.approved_amount <= 0:
        raise FineractServerError(200, {}, "Fineract's approval template has no amount")
    return form


def find_approval(
    fineract: FineractApi, loan_reference: str, correlation_id: str | None
) -> FineractLoan | None:
    """The loan if Fineract shows it approved (or further on), else None."""
    loan = find_loan(fineract, loan_reference, correlation_id)
    if loan is None or loan.approved_on is None or loan.status_id not in _APPROVED_OR_LATER:
        return None
    return loan


def _finish_approval(
    engine: Engine,
    op: Operation,
    client_uid: str,
    loan: FineractLoan,
    correlation_id: str | None,
) -> Answer:
    assert op.loan_reference is not None  # APR rows always carry one (CHECK constraint)
    approved_on = loan.approved_on or kigali_today()
    approved_principal = loan.approved_principal or loan.principal or 0
    body = approval_body(
        op.loan_reference, client_uid, approved_on, approved_principal, op.external_reference
    )
    with engine.begin() as conn:
        conn.execute(
            update(loans)
            .where(loans.c.loan_reference == op.loan_reference)
            .values(status=APPROVED, updated_at=datetime.now(UTC))
        )
        mark_fineract_done(
            conn,
            op.id,
            response_status=200,
            response_body=body,
            fineract_resource_id=loan.fineract_id,
            detail="approved in Fineract",
            correlation_id=correlation_id,
        )
    return Answer(200, body)


def recover_approval(
    engine: Engine, fineract: FineractApi, op: Operation, correlation_id: str | None = None
) -> str:
    """For recovery: an APR left FINERACT_PENDING. Looks it up; never sends it again."""
    assert op.loan_reference is not None
    try:
        loan = find_approval(fineract, op.loan_reference, correlation_id)
    except FineractError:
        return "unknown"
    if loan is None:
        mark_failed(engine, op.id, "recovery lookup found no approval", correlation_id)
        return "failed"
    _finish_approval(engine, op, op.client_uid or loan.client_uid or "", loan, correlation_id)
    return "done"


# --- the loan as Fineract reports it ----------------------------------------------------------


def loan_details(
    engine: Engine, fineract: FineractApi, loan_reference: str, correlation_id: str | None
) -> dict[str, Any]:
    loan = our_loan(engine, loan_reference)
    if loan is None:
        raise ApiError(ErrorCode.LOAN_NOT_FOUND, f"No loan {loan_reference}.", 404)
    try:
        data = get_fineract_loan(fineract, loan_reference, correlation_id)
    except FineractError as exc:
        raise fineract_read_error(exc) from exc
    if data is None:
        raise ApiError(ErrorCode.LOAN_NOT_FOUND, f"Fineract has no loan {loan_reference}.", 404)
    payoff = None
    if loan_status_code((data.get("status") or {}).get("code")) == ACTIVE:
        payoff = payoff_amount(fineract, loan_reference, correlation_id)
    return loan_view(data, loan_reference, loan.client_uid, payoff)


def payoff_amount(
    fineract: FineractApi, loan_reference: str, correlation_id: str | None
) -> int | None:
    """What clears the loan today, from Fineract's prepayLoan template. Best effort: a failed
    read or a template we cannot read gives None."""
    try:
        return fetch_payoff_form(fineract, loan_reference, correlation_id).amount
    except FineractError:
        return None
