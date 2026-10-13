"""Chart of accounts: create (idempotent), list, and balance. No Flask here.

Hierarchy rules:
- A parent, when given, must exist, be a HEADER, and have the same type.
- A HEADER may have no parent, or a HEADER parent.
- A DETAIL must have a parent. Every seeded DETAIL sits under a HEADER, and this keeps
  every postable account inside one of the top-level groups.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.errors import UniqueViolation
from sqlalchemy import Connection, Engine, RowMapping, text
from sqlalchemy.exc import IntegrityError

from mock_erp.errors import ErpError, ErrorCode
from mock_erp.hashing import request_hash
from mock_erp.schemas.accounts import (
    Account,
    AccountBalance,
    AccountCreated,
    AccountUsage,
    CreateAccountRequest,
    NormalSide,
)

DEBIT_NORMAL_TYPES = frozenset({"ASSET", "EXPENSE"})


@dataclass(frozen=True)
class ParentInfo:
    """What the hierarchy rule needs to know about a parent account."""

    usage: str
    type: str


def check_parent(
    usage: str, type_: str, parent_code: str | None, parent: ParentInfo | None
) -> None:
    """Raise parent_invalid if the account may not sit where it asks to.
    `parent` is the stored parent, or None when it was not found."""
    if parent_code is None:
        if usage == "DETAIL":
            raise _parent_invalid("A DETAIL account needs a HEADER parent.", None)
        return
    if parent is None:
        raise _parent_invalid(f"Parent account {parent_code} does not exist.", parent_code)
    if parent.usage != "HEADER":
        raise _parent_invalid(f"Parent account {parent_code} is not a HEADER.", parent_code)
    if parent.type != type_:
        raise _parent_invalid(
            f"Parent account {parent_code} is {parent.type}, not {type_}.", parent_code
        )


def _parent_invalid(message: str, parent_code: str | None) -> ErpError:
    return ErpError(ErrorCode.PARENT_INVALID, message, 422, {"parent_code": parent_code})


def normal_side(type_: str) -> NormalSide:
    return "DEBIT" if type_ in DEBIT_NORMAL_TYPES else "CREDIT"


def balance_for(type_: str, *, debit_total: int, credit_total: int) -> int:
    """Normal-side total minus the other side."""
    if normal_side(type_) == "DEBIT":
        return debit_total - credit_total
    return credit_total - debit_total


def hash_fields(req: CreateAccountRequest) -> dict[str, Any]:
    """The fields that make two create requests the same. The seed hashes these too."""
    return {
        "code": req.code,
        "external_reference": req.external_reference,
        "name": req.name,
        "type": req.type,
        "usage": req.usage,
        "parent_code": req.parent_code,
        "currency": req.currency,
    }


def account_hash(req: CreateAccountRequest) -> str:
    return request_hash(hash_fields(req))


# Database work


def create_account(engine: Engine, req: CreateAccountRequest) -> AccountCreated:
    """Create the account, or return the stored one if this exact request was seen before.
    Raises parent_invalid (422), reference_conflict (409) or code_taken (409)."""
    req_hash = account_hash(req)
    try:
        with engine.begin() as conn:
            _check_parent_in_db(conn, req)
            row = (
                conn.execute(
                    text(
                        "INSERT INTO accounts (code, external_reference, name, type, usage, "
                        "parent_code, currency, request_hash) VALUES (:code, "
                        ":external_reference, :name, :type, :usage, :parent_code, :currency, "
                        ":request_hash) ON CONFLICT (external_reference) DO NOTHING "
                        "RETURNING code, external_reference, name, type, usage, parent_code, "
                        "currency, created_at"
                    ),
                    {**hash_fields(req), "request_hash": req_hash},
                )
                .mappings()
                .first()
            )
    except IntegrityError as exc:
        if not _is_code_violation(exc):
            raise
        # The code is used. If a twin request won the race, the reference is there now.
        existing = _find_by_reference(engine, req.external_reference)
        if existing is None:
            raise ErpError(
                ErrorCode.CODE_TAKEN,
                f"Account code {req.code} is already used by another reference.",
                409,
                {"code": req.code},
            ) from exc
        return _replay(existing, req_hash)
    if row is not None:
        return AccountCreated.model_validate({**row, "status": "CREATED"})
    existing = _find_by_reference(engine, req.external_reference)
    if existing is None:  # pragma: no cover - the conflicting row is committed by now
        raise RuntimeError(f"account {req.external_reference} conflicted but is not there")
    return _replay(existing, req_hash)


def _check_parent_in_db(conn: Connection, req: CreateAccountRequest) -> None:
    parent: ParentInfo | None = None
    if req.parent_code is not None:
        found = (
            conn.execute(
                text("SELECT usage, type FROM accounts WHERE code = :code"),
                {"code": req.parent_code},
            )
            .mappings()
            .first()
        )
        if found is not None:
            parent = ParentInfo(usage=found["usage"], type=found["type"])
    check_parent(req.usage, req.type, req.parent_code, parent)


def _is_code_violation(exc: IntegrityError) -> bool:
    orig = exc.orig
    return isinstance(orig, UniqueViolation) and orig.diag.constraint_name == "accounts_pkey"


def _find_by_reference(engine: Engine, external_reference: str) -> RowMapping | None:
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT code, external_reference, name, type, usage, parent_code, "
                    "currency, created_at, request_hash FROM accounts "
                    "WHERE external_reference = :ref"
                ),
                {"ref": external_reference},
            )
            .mappings()
            .first()
        )


def _replay(existing: RowMapping, req_hash: str) -> AccountCreated:
    if existing["request_hash"] != req_hash:
        raise ErpError(
            ErrorCode.REFERENCE_CONFLICT,
            f"Reference {existing['external_reference']} was already used with different data.",
            409,
            {"external_reference": existing["external_reference"]},
        )
    return AccountCreated.model_validate({**existing, "status": "ALREADY_EXISTS"})


def list_accounts(engine: Engine, usage: AccountUsage | None = None) -> list[Account]:
    """Every account, each HEADER followed by its children."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT code, external_reference, name, type, usage, parent_code, currency, "
                "created_at FROM accounts "
                "WHERE CAST(:usage AS text) IS NULL OR usage = :usage "
                "ORDER BY coalesce(parent_code, code), parent_code NULLS FIRST, code"
            ),
            {"usage": usage},
        ).mappings()
        return [Account.model_validate(row) for row in rows]


def get_balance(engine: Engine, code: str) -> AccountBalance:
    """Debit and credit totals for the account and everything under it, at any depth.
    Raises not_found (404) for an unknown code."""
    with engine.connect() as conn:
        account = (
            conn.execute(
                text("SELECT code, name, type, usage FROM accounts WHERE code = :code"),
                {"code": code},
            )
            .mappings()
            .first()
        )
        if account is None:
            raise ErpError(
                ErrorCode.NOT_FOUND, f"Account {code} does not exist.", 404, {"code": code}
            )
        totals = conn.execute(
            text(
                "WITH RECURSIVE tree AS ("
                "  SELECT code FROM accounts WHERE code = :code"
                "  UNION ALL"
                "  SELECT a.code FROM accounts a JOIN tree t ON a.parent_code = t.code"
                ") "
                "SELECT coalesce(sum(l.debit), 0) AS debit_total, "
                "       coalesce(sum(l.credit), 0) AS credit_total "
                "FROM journal_lines l JOIN tree t ON l.account_code = t.code"
            ),
            {"code": code},
        ).one()
    debit_total, credit_total = int(totals.debit_total), int(totals.credit_total)
    return AccountBalance(
        code=account["code"],
        name=account["name"],
        type=account["type"],
        usage=account["usage"],
        debit_total=debit_total,
        credit_total=credit_total,
        balance=balance_for(account["type"], debit_total=debit_total, credit_total=credit_total),
        normal_side=normal_side(account["type"]),
    )
