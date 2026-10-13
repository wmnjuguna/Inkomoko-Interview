"""Part H: the ERP journal entry for a disbursement or a repayment.

Built only from Fineract's own transaction (the one Fineract answered with, or the one a
lookup by externalId found): its amount, its date and how Fineract split it. Nothing is
computed on our side, so the ERP entry always matches what Fineract posted.

- Disbursement: DR loan portfolio (1200) / CR bank (1100), the amount.
- Repayment: DR bank (1100) the amount / CR loan portfolio (1200) the principal portion /
  CR interest income (4100) the interest portion / CR overpayment liability (2100) the
  overpayment portion. Zero lines are left out.

What is never guessed (NeedsReconciliation): fee or penalty portions, a reversed
transaction, portions that do not add up to the amount, an unexpected transaction type,
another currency, fractions of a franc, or a transaction whose externalId is not ours.

The account codes come from the product's GL roles in fineract/configuration/loan-product.json
(e.g. fundSourceAccountId -> GL-1100). The ERP reference GL-1100 is the ERP account with
code 1100, the account GL sync created in Fineract, so the ERP entry uses the same accounts
Fineract posts to.

Pure: no database and no HTTP, except load_gl_role_accounts(), which reads one file."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DISBURSEMENT_TYPE = "loanTransactionType.disbursement"
REPAYMENT_TYPE = "loanTransactionType.repayment"
CURRENCY = "RWF"

_SOURCES: dict[str, tuple[str, str, str]] = {
    # op type: (Fineract transaction type, ERP source, description word)
    "DSB": (DISBURSEMENT_TYPE, "DISBURSEMENT", "Disbursement"),
    "RPY": (REPAYMENT_TYPE, "REPAYMENT", "Repayment"),
}

ErpStatus = Literal["PENDING", "NEEDS_RECONCILIATION"]


class NeedsReconciliation(Exception):  # noqa: N818 (the name the plan uses)
    """Fineract's transaction cannot be turned into an entry without guessing.
    reason is in our own words and holds no Fineract ids."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GlRolesConfigError(RuntimeError):
    """The product's GL role accounts could not be read."""


# --- Fineract's transaction ---------------------------------------------------------------


class _TxnType(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    code: str


class _Currency(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    code: str


class FineractTransaction(BaseModel):
    """The fields of a Fineract loan transaction that the entry is built from. Fineract's
    numeric ids are kept out on purpose."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    type: _TxnType
    transaction_date: date = Field(alias="date")
    amount: Decimal
    currency: _Currency | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    principal_portion: Decimal = Field(default=Decimal(0), alias="principalPortion")
    interest_portion: Decimal = Field(default=Decimal(0), alias="interestPortion")
    fee_charges_portion: Decimal = Field(default=Decimal(0), alias="feeChargesPortion")
    penalty_charges_portion: Decimal = Field(default=Decimal(0), alias="penaltyChargesPortion")
    overpayment_portion: Decimal = Field(default=Decimal(0), alias="overpaymentPortion")
    unrecognized_income_portion: Decimal = Field(
        default=Decimal(0), alias="unrecognizedIncomePortion"
    )
    manually_reversed: bool = Field(default=False, alias="manuallyReversed")
    reversal_external_id: str | None = Field(default=None, alias="reversalExternalId")
    reversed_on_date: date | None = Field(default=None, alias="reversedOnDate")

    @field_validator("transaction_date", "reversed_on_date", mode="before")
    @classmethod
    def _date_array(cls, value: Any) -> Any:
        """Fineract sends dates as [year, month, day]."""
        if isinstance(value, list | tuple) and len(value) == 3:
            return date(int(value[0]), int(value[1]), int(value[2]))
        return value

    @field_validator(
        "principal_portion",
        "interest_portion",
        "fee_charges_portion",
        "penalty_charges_portion",
        "overpayment_portion",
        "unrecognized_income_portion",
        mode="before",
    )
    @classmethod
    def _none_is_zero(cls, value: Any) -> Any:
        return 0 if value is None else value

    @property
    def type_code(self) -> str:
        return self.type.code

    @property
    def reversed(self) -> bool:
        return (
            self.manually_reversed
            or self.reversal_external_id is not None
            or self.reversed_on_date is not None
        )


# --- accounts ------------------------------------------------------------------------------


@dataclass(frozen=True)
class GlRoleAccounts:
    """ERP account codes for the GL roles a loan posts to."""

    bank: str
    loan_portfolio: str
    interest_income: str
    overpayment_liability: str


# loan-product.json glAccounts role -> GlRoleAccounts field
GL_ROLES = {
    "fundSourceAccountId": "bank",
    "loanPortfolioAccountId": "loan_portfolio",
    "interestOnLoanAccountId": "interest_income",
    "overpaymentLiabilityAccountId": "overpayment_liability",
}
_GL_REFERENCE = re.compile(r"^GL-(\d+)$")


def load_gl_role_accounts(path: Path | str) -> GlRoleAccounts:
    """Reads glAccounts from the loan product file. 'GL-1100' gives the code '1100'."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GlRolesConfigError(f"Loan product file not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise GlRolesConfigError(f"Loan product file {path} is not readable JSON") from exc
    roles = data.get("glAccounts") if isinstance(data, dict) else None
    if not isinstance(roles, dict):
        raise GlRolesConfigError(f"{path} has no glAccounts")
    codes: dict[str, str] = {}
    for role, field in GL_ROLES.items():
        reference = roles.get(role)
        match = _GL_REFERENCE.match(reference) if isinstance(reference, str) else None
        if match is None:
            raise GlRolesConfigError(f"{path}: glAccounts.{role} must look like GL-<code>")
        codes[field] = match.group(1)
    return GlRoleAccounts(**codes)


# --- the entry -----------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalLine:
    account_code: str
    debit: int = 0
    credit: int = 0

    def to_erp(self) -> dict[str, Any]:
        side = "debit" if self.debit else "credit"
        return {"account_code": self.account_code, side: self.debit or self.credit}


@dataclass(frozen=True)
class JournalEntry:
    external_reference: str
    entry_date: date
    source: str
    description: str
    lines: tuple[JournalLine, ...]

    def to_erp_body(self) -> dict[str, Any]:
        """The body for the ERP's POST /api/journal-entries."""
        return {
            "external_reference": self.external_reference,
            "entry_date": self.entry_date.isoformat(),
            "source": self.source,
            "description": self.description,
            "lines": [line.to_erp() for line in self.lines],
        }


def build_entry(
    op_type: str,
    op_key: str,
    fineract_txn: FineractTransaction,
    accounts: GlRoleAccounts,
    loan_reference: str,
) -> JournalEntry:
    """The ERP entry for Fineract's transaction, or NeedsReconciliation."""
    if op_type not in _SOURCES:
        raise ValueError(f"No journal entry for operation type {op_type}")
    expected_type, source, word = _SOURCES[op_type]
    t = fineract_txn
    if t.type_code != expected_type:
        raise NeedsReconciliation(f"Fineract transaction type is not a {word.lower()}")
    if t.external_id != op_key:
        raise NeedsReconciliation("Fineract transaction externalId is not this operation's")
    if t.reversed:
        raise NeedsReconciliation("Fineract transaction is reversed")
    if t.currency is not None and t.currency.code != CURRENCY:
        raise NeedsReconciliation(f"Fineract transaction is not in {CURRENCY}")
    if t.fee_charges_portion or t.penalty_charges_portion:
        raise NeedsReconciliation("Fineract transaction has a fee or penalty portion")
    if t.unrecognized_income_portion:
        raise NeedsReconciliation("Fineract transaction has an unrecognized income portion")

    amount = _whole(t.amount)
    principal = _whole(t.principal_portion)
    interest = _whole(t.interest_portion)
    overpayment = _whole(t.overpayment_portion)
    if amount <= 0:
        raise NeedsReconciliation("Fineract transaction amount is not above zero")

    if op_type == "DSB":
        if interest or overpayment or principal not in (0, amount):
            raise NeedsReconciliation("Fineract disbursement has unexpected portions")
        lines = [
            JournalLine(accounts.loan_portfolio, debit=amount),
            JournalLine(accounts.bank, credit=amount),
        ]
    else:
        if principal + interest + overpayment != amount:
            raise NeedsReconciliation("Fineract repayment portions do not add up to the amount")
        credits = [
            JournalLine(accounts.loan_portfolio, credit=principal),
            JournalLine(accounts.interest_income, credit=interest),
            JournalLine(accounts.overpayment_liability, credit=overpayment),
        ]
        lines = [JournalLine(accounts.bank, debit=amount)] + [c for c in credits if c.credit]

    return JournalEntry(
        external_reference=op_key,
        entry_date=t.transaction_date,
        source=source,
        description=f"{word} {loan_reference}",
        lines=tuple(lines),
    )


def _whole(value: Decimal) -> int:
    if value != value.to_integral_value():
        raise NeedsReconciliation("Fineract amounts are not whole francs")
    return int(value)


# --- plan: what goes in the outbox ---------------------------------------------------------


@dataclass(frozen=True)
class JournalPlan:
    """The entry to send, or why there is none. erp_status is what the partner sees."""

    external_reference: str
    entry: JournalEntry | None
    problem: str | None

    @property
    def erp_status(self) -> ErpStatus:
        return "PENDING" if self.entry is not None else "NEEDS_RECONCILIATION"

    def outbox_entry(self) -> dict[str, Any]:
        """The outbox row's entry: the ERP body, or a marker that nothing can be sent."""
        if self.entry is not None:
            return self.entry.to_erp_body()
        return {
            "external_reference": self.external_reference,
            "unbuilt": True,
            "reason": self.problem,
        }


def plan_entry(
    op_type: str,
    op_key: str,
    fineract_txn: FineractTransaction,
    accounts: GlRoleAccounts,
    loan_reference: str,
) -> JournalPlan:
    """build_entry(), with NeedsReconciliation turned into a plan without an entry."""
    try:
        entry = build_entry(op_type, op_key, fineract_txn, accounts, loan_reference)
    except NeedsReconciliation as exc:
        return JournalPlan(op_key, None, exc.reason)
    return JournalPlan(op_key, entry, None)


def parse_transaction(data: Any) -> FineractTransaction:
    """Fineract's transaction body. A body we cannot read raises NeedsReconciliation."""
    try:
        return FineractTransaction.model_validate(data)
    except ValidationError as exc:
        raise NeedsReconciliation("Fineract transaction could not be read") from exc
