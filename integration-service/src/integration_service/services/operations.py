"""The idempotency engine: one row per partner request in `operations`, enforced by Postgres.

begin() claims a request, or tells the caller what to answer instead:
- New(operation): run it. The row is FINERACT_PENDING and operation.attempts is the attempt
  number for the Fineract Idempotency-Key (<op key>:<attempt>).
- Replay(status, body): finished before. Answer with the exact saved status and body. The
  live status is at GET /api/transactions/{external_reference}.
- InProgress(operation): the same request is still running. Answer 202, never run it twice.
- Conflict(): the reference was used with other data (or by another partner). 409.
- Taken(code, holder_status): another reference already holds the one live client creation
  for this client_uid, or the one live approval or disbursement for this loan. 409 with code.
  The caller's reference is not saved, so it can be used again once the holder has FAILED.
  I chose 409 over 202 here even while the holder runs: the caller's own reference was not
  accepted, and 202 would suggest it was.

How the database enforces it (nothing is kept in memory):
- INSERT ... ON CONFLICT (op_type, external_reference) DO NOTHING. Two requests at once:
  the second waits for the first to commit, then reads its row.
- Partial unique indexes allow one live (not FAILED) CLI per client_uid, APR per loan and
  DSB per loan. A second reference fails the insert with a unique violation: Taken.
- A FAILED row is retried on the same row (FAILED -> FINERACT_PENDING, attempts + 1) under a
  row lock, so ten retries at once make exactly one New.

The request hash covers the partner and the payload, so another partner reusing a reference
gets a conflict, never someone else's saved answer.

Every status change goes through state_machine.transition() and is written to
operation_events in the same transaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, RowMapping, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from integration_service.errors import ErrorCode
from integration_service.hashing import request_hash
from integration_service.logs import log_event
from integration_service.models import OP_TYPES, operation_events, operations, outbox
from integration_service.state_machine import (
    COMPLETED,
    ERP_PENDING,
    FAILED,
    FINERACT_DONE,
    FINERACT_PENDING,
    FINISHED,
    RECEIVED,
    transition,
)

TYPE_NAMES = {
    "CLI": "CLIENT_CREATION",
    "APP": "LOAN_APPLICATION",
    "APR": "APPROVAL",
    "DSB": "DISBURSEMENT",
    "RPY": "REPAYMENT",
}

# Partial unique index -> the partner error when another reference holds it.
TAKEN_RULES = {
    "operations_one_live_client": ErrorCode.CLIENT_UID_TAKEN,
    "operations_one_live_approval": ErrorCode.LOAN_ALREADY_APPROVED,
    "operations_one_live_disbursement": ErrorCode.LOAN_ALREADY_DISBURSED,
}
_TAKEN_LOOKUP = {
    "operations_one_live_client": ("CLI", "client_uid"),
    "operations_one_live_approval": ("APR", "loan_reference"),
    "operations_one_live_disbursement": ("DSB", "loan_reference"),
}

# op_key and (op_type, external_reference) are two unique rules for the same thing.
# ON CONFLICT names only one, so when two inserts race the other one can raise a unique
# violation instead. Postgres raises it only after the first insert committed, so reading
# again in a new transaction finds the row.
_SAME_KEY_RULES = frozenset({"operations_op_key_key", "operations_type_reference_key"})
_SAME_KEY_TRIES = 3

# Columns move() and set_fields() may change besides the status.
_CHANGEABLE = frozenset(
    {
        "attempts",
        "recovery_lookups",
        "next_attempt_at",
        "last_error",
        "fineract_resource_id",
        "fineract_transaction_id",
        "erp_status",
        "erp_transaction_id",
        "client_uid",
        "loan_reference",
    }
)


@dataclass(frozen=True)
class Operation:
    """One row of operations. fineract_* ids are internal: never put them in a response."""

    id: int
    op_type: str
    external_reference: str
    op_key: str
    partner: str
    status: str
    client_uid: str | None
    loan_reference: str | None
    attempts: int
    recovery_lookups: int
    request_payload: Any
    next_attempt_at: datetime | None
    last_error: str | None
    response_status: int | None
    response_body: Any
    fineract_resource_id: int | None
    fineract_transaction_id: int | None
    erp_status: str | None
    erp_transaction_id: str | None
    correlation_id: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def idempotency_key(self) -> str:
        """The Fineract Idempotency-Key for the current attempt."""
        return f"{self.op_key}:{self.attempts}"

    @classmethod
    def from_row(cls, row: RowMapping) -> Operation:
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class New:
    operation: Operation


@dataclass(frozen=True)
class Replay:
    status_code: int
    body: Any


@dataclass(frozen=True)
class InProgress:
    operation: Operation | None = None


@dataclass(frozen=True)
class Conflict:
    pass


@dataclass(frozen=True)
class Taken:
    code: ErrorCode
    holder_status: str | None


@dataclass(frozen=True)
class Retry:
    """decide(): a FAILED request sent again with the same data. begin() turns it into New."""


BeginResult = New | Replay | InProgress | Conflict | Taken


@dataclass(frozen=True)
class Event:
    from_status: str | None
    to_status: str
    at: datetime
    detail: str | None


@dataclass(frozen=True)
class OperationView:
    """What GET /api/transactions shows. No Fineract ids."""

    op_type: str
    op_key: str
    status: str
    erp_status: str | None
    client_uid: str | None
    loan_reference: str | None
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    history: list[Event]

    @property
    def type_name(self) -> str:
        return TYPE_NAMES[self.op_type]


class StaleStatusError(RuntimeError):
    """The operation is no longer in the expected status. Nothing was changed."""


class ResponseAlreadySavedError(RuntimeError):
    """The saved answer is never replaced: repeats must get exactly the first one."""


# --- pure rules -----------------------------------------------------------------------------


def make_op_key(op_type: str, external_reference: str) -> str:
    if op_type not in OP_TYPES:
        raise ValueError(f"Unknown operation type {op_type}")
    return f"{op_type}:{external_reference}"


def operation_hash(partner: str, payload: Mapping[str, Any]) -> str:
    return request_hash({"partner": partner, "payload": dict(payload)})


def format_loan_reference(year: int, number: int) -> str:
    return f"LN-{year}-{number:06d}"


def decide(
    status: str,
    saved_hash: str,
    new_hash: str,
    response_status: int | None,
    response_body: Any,
) -> Replay | InProgress | Conflict | Retry:
    """What a repeated (op_type, external_reference) gets, given the saved row."""
    if saved_hash != new_hash:
        return Conflict()
    if status == FAILED:
        return Retry()
    if status in FINISHED and response_status is not None and response_body is not None:
        return Replay(response_status, response_body)
    return InProgress()


# --- begin ----------------------------------------------------------------------------------


def begin(
    engine: Engine,
    op_type: str,
    external_reference: str,
    payload: Mapping[str, Any],
    partner: str,
    correlation_id: str | None,
    client_uid: str | None = None,
    loan_reference: str | None = None,
) -> BeginResult:
    key = make_op_key(op_type, external_reference)
    digest = operation_hash(partner, payload)
    for tries_left in reversed(range(_SAME_KEY_TRIES)):
        try:
            with engine.begin() as conn:
                result = _claim(
                    conn,
                    op_type,
                    external_reference,
                    key,
                    digest,
                    payload,
                    partner,
                    correlation_id,
                    client_uid,
                    loan_reference,
                )
            break
        except IntegrityError as exc:
            rule = _constraint_name(exc)
            if rule in _SAME_KEY_RULES and tries_left:
                continue  # the other insert has committed by now: read its row
            if rule not in TAKEN_RULES:
                raise
            holder = _holder_status(engine, rule, client_uid, loan_reference)
            result = Taken(TAKEN_RULES[rule], holder)
            break
    _log_begin(result, key, external_reference, client_uid, loan_reference, correlation_id)
    return result


def _claim(
    conn: Connection,
    op_type: str,
    external_reference: str,
    key: str,
    digest: str,
    payload: Mapping[str, Any],
    partner: str,
    correlation_id: str | None,
    client_uid: str | None,
    loan_reference: str | None,
) -> BeginResult:
    inserted = (
        conn.execute(
            pg_insert(operations)
            .values(
                op_type=op_type,
                external_reference=external_reference,
                op_key=key,
                partner=partner,
                request_hash=digest,
                request_payload=dict(payload),
                status=RECEIVED,
                client_uid=client_uid,
                loan_reference=loan_reference,
                correlation_id=correlation_id,
            )
            .on_conflict_do_nothing(index_elements=["op_type", "external_reference"])
            .returning(operations.c.id)
        )
        .scalars()
        .one_or_none()
    )
    if inserted is not None:
        _add_event(conn, inserted, None, RECEIVED, "request saved", correlation_id)
        return New(
            move(conn, inserted, FINERACT_PENDING, "saved and claimed", correlation_id, attempts=1)
        )

    row = (
        conn.execute(
            select(operations)
            .where(operations.c.op_type == op_type)
            .where(operations.c.external_reference == external_reference)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    decision = decide(
        row["status"], row["request_hash"], digest, row["response_status"], row["response_body"]
    )
    if isinstance(decision, Retry):
        retried = move(
            conn,
            row["id"],
            FINERACT_PENDING,
            "retried with the same reference",
            correlation_id,
            expected=FAILED,
            attempts=row["attempts"] + 1,
            recovery_lookups=0,
            last_error=None,
            next_attempt_at=None,
        )
        return New(retried)
    if isinstance(decision, InProgress):
        return InProgress(Operation.from_row(row))
    return decision


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def _holder_status(
    engine: Engine, rule: str, client_uid: str | None, loan_reference: str | None
) -> str | None:
    op_type, column = _TAKEN_LOOKUP[rule]
    value = client_uid if column == "client_uid" else loan_reference
    with engine.connect() as conn:
        status: str | None = conn.execute(
            select(operations.c.status)
            .where(operations.c.op_type == op_type)
            .where(operations.c[column] == value)
            .where(operations.c.status != FAILED)
        ).scalar_one_or_none()
    return status


def _log_begin(
    result: BeginResult,
    key: str,
    external_reference: str,
    client_uid: str | None,
    loan_reference: str | None,
    correlation_id: str | None,
) -> None:
    status = None
    if isinstance(result, New | InProgress) and result.operation is not None:
        status = result.operation.status
    log_event(
        "operation begin",
        correlation_id=correlation_id,
        op_key=key,
        external_reference=external_reference,
        client_uid=client_uid,
        loan_reference=loan_reference,
        result=type(result).__name__,
        status=status,
    )


# --- status changes -------------------------------------------------------------------------


def move(
    engine_or_conn: Engine | Connection,
    operation_id: int,
    target: str,
    detail: str | None,
    correlation_id: str | None = None,
    *,
    expected: str | None = None,
    **values: Any,
) -> Operation:
    """Move to target if the state machine allows it from the current status (and the
    current status is `expected`, when given). Records the event in the same transaction.
    Extra keyword values update other columns (attempts, last_error, erp_status, ...)."""
    if isinstance(engine_or_conn, Engine):
        with engine_or_conn.begin() as conn:
            return move(
                conn, operation_id, target, detail, correlation_id, expected=expected, **values
            )
    conn = engine_or_conn
    _check_columns(values)
    current = conn.execute(
        select(operations.c.status).where(operations.c.id == operation_id).with_for_update()
    ).scalar_one()
    if expected is not None and current != expected:
        raise StaleStatusError(f"operation {operation_id} is {current}, not {expected}")
    transition(current, target)
    row = (
        conn.execute(
            update(operations)
            .where(operations.c.id == operation_id)
            .where(operations.c.status == current)
            .values(status=target, updated_at=datetime.now(UTC), **values)
            .returning(operations)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise StaleStatusError(f"operation {operation_id} changed while moving to {target}")
    _add_event(conn, operation_id, current, target, detail, correlation_id)
    operation = Operation.from_row(row)
    log_event(
        "operation status",
        correlation_id=correlation_id or operation.correlation_id,
        op_key=operation.op_key,
        external_reference=operation.external_reference,
        client_uid=operation.client_uid,
        loan_reference=operation.loan_reference,
        fineract_transaction_id=operation.fineract_transaction_id,
        erp_transaction_id=operation.erp_transaction_id,
        status=target,
        previous_status=current,
        detail=detail,
    )
    return operation


def mark_failed(
    engine_or_conn: Engine | Connection,
    operation_id: int,
    reason: str,
    correlation_id: str | None = None,
) -> Operation:
    """FINERACT_PENDING -> FAILED. Only when we know nothing happened in Fineract.
    reason is shown to the partner: our own words only."""
    return move(
        engine_or_conn,
        operation_id,
        FAILED,
        reason,
        correlation_id,
        expected=FINERACT_PENDING,
        last_error=reason,
    )


def set_fields(engine_or_conn: Engine | Connection, operation_id: int, **values: Any) -> None:
    """Change columns without a status change, e.g. last_error and next_attempt_at while an
    unclear outcome waits for recovery."""
    _check_columns(values)
    statement = (
        update(operations)
        .where(operations.c.id == operation_id)
        .values(updated_at=datetime.now(UTC), **values)
    )
    if isinstance(engine_or_conn, Engine):
        with engine_or_conn.begin() as conn:
            conn.execute(statement)
    else:
        engine_or_conn.execute(statement)


def save_response(conn: Connection, operation_id: int, status_code: int, body: Any) -> None:
    """Save the answer that every repeat of this request gets. Never replaced."""
    saved = conn.execute(
        update(operations)
        .where(operations.c.id == operation_id)
        .where(operations.c.response_body.is_(None))
        .values(response_status=status_code, response_body=body, updated_at=datetime.now(UTC))
        .returning(operations.c.id)
    ).one_or_none()
    if saved is None:
        raise ResponseAlreadySavedError(f"operation {operation_id} already has its answer")


def mark_fineract_done(
    conn: Connection,
    operation_id: int,
    *,
    response_status: int,
    response_body: Any,
    fineract_resource_id: int | None = None,
    fineract_transaction_id: int | None = None,
    outbox_entry: Mapping[str, Any] | None = None,
    detail: str = "Fineract said yes",
    correlation_id: str | None = None,
) -> Operation:
    """In the caller's transaction: FINERACT_DONE with the Fineract ids and the saved answer,
    then either an outbox row and ERP_PENDING (erp_status PENDING), or COMPLETED.
    Commit it all or nothing: a crash before the commit leaves FINERACT_PENDING for recovery."""
    move(
        conn,
        operation_id,
        FINERACT_DONE,
        detail,
        correlation_id,
        expected=FINERACT_PENDING,
        fineract_resource_id=fineract_resource_id,
        fineract_transaction_id=fineract_transaction_id,
        last_error=None,
        next_attempt_at=None,
    )
    save_response(conn, operation_id, response_status, response_body)
    if outbox_entry is None:
        return move(conn, operation_id, COMPLETED, "nothing to send to the ERP", correlation_id)
    conn.execute(insert(outbox).values(operation_id=operation_id, entry=dict(outbox_entry)))
    return move(
        conn,
        operation_id,
        ERP_PENDING,
        "journal entry waiting for the ERP",
        correlation_id,
        erp_status="PENDING",
    )


def _add_event(
    conn: Connection,
    operation_id: int,
    from_status: str | None,
    to_status: str,
    detail: str | None,
    correlation_id: str | None,
) -> None:
    conn.execute(
        insert(operation_events).values(
            operation_id=operation_id,
            from_status=from_status,
            to_status=to_status,
            detail=detail,
            correlation_id=correlation_id,
        )
    )


def _check_columns(values: Mapping[str, Any]) -> None:
    unknown = set(values) - _CHANGEABLE
    if unknown:
        raise ValueError(f"Cannot change {sorted(unknown)} this way")


# --- reads ----------------------------------------------------------------------------------


def get_operation(engine: Engine, op_type: str, external_reference: str) -> Operation | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(operations)
                .where(operations.c.op_type == op_type)
                .where(operations.c.external_reference == external_reference)
            )
            .mappings()
            .one_or_none()
        )
    return Operation.from_row(row) if row is not None else None


def list_for_reference(
    engine: Engine, external_reference: str, partner: str
) -> list[OperationView]:
    """Every operation this partner sent with this reference, in type order, with history.
    Another partner's operations are not shown."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(operations)
                .where(operations.c.external_reference == external_reference)
                .where(operations.c.partner == partner)
                .order_by(operations.c.id)
            )
            .mappings()
            .all()
        )
        ids = [row["id"] for row in rows]
        events = (
            conn.execute(
                select(operation_events)
                .where(operation_events.c.operation_id.in_(ids))
                .order_by(operation_events.c.id)
            )
            .mappings()
            .all()
            if ids
            else []
        )
    by_operation: dict[int, list[Event]] = {i: [] for i in ids}
    for event in events:
        by_operation[event["operation_id"]].append(
            Event(event["from_status"], event["to_status"], event["at"], event["detail"])
        )
    return [
        OperationView(
            op_type=row["op_type"],
            op_key=row["op_key"],
            status=row["status"],
            erp_status=row["erp_status"],
            client_uid=row["client_uid"],
            loan_reference=row["loan_reference"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            history=by_operation[row["id"]],
        )
        for row in rows
    ]


def next_loan_reference(conn: Connection) -> str:
    """LN-<year>-<number>, the year from the database's current date, the number from
    loan_reference_seq. A number is never reused, so gaps are possible."""
    row = conn.execute(
        text("SELECT extract(year FROM current_date)::int, nextval('loan_reference_seq')")
    ).one()
    return format_loan_reference(int(row[0]), int(row[1]))
