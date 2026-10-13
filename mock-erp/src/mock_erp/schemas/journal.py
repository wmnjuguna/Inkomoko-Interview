"""Journal entry models: what a caller sends and what the ERP sends back."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

MAX_AMOUNT = 10**15
REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:_.\-]*$"

EntrySource = Literal["DISBURSEMENT", "REPAYMENT", "REVERSAL", "MANUAL"]
Amount = StrictInt


class JournalLineIn(BaseModel):
    """One line. Whole RWF; exactly one of debit or credit is above zero."""

    model_config = ConfigDict(extra="forbid")

    account_code: str = Field(min_length=1, max_length=20)
    debit: Amount = Field(default=0, ge=0, le=MAX_AMOUNT)
    credit: Amount = Field(default=0, ge=0, le=MAX_AMOUNT)

    @model_validator(mode="after")
    def one_side(self) -> Self:
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("a line needs exactly one of debit or credit above zero")
        return self


class JournalEntryIn(BaseModel):
    """A balanced entry. Only a REVERSAL names the entry it reverses."""

    model_config = ConfigDict(extra="forbid")

    external_reference: str = Field(min_length=1, max_length=64, pattern=REFERENCE_PATTERN)
    entry_date: date
    source: EntrySource
    description: str | None = Field(default=None, max_length=500)
    reverses: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=REFERENCE_PATTERN
    )
    lines: list[JournalLineIn] = Field(min_length=2, max_length=50)

    @model_validator(mode="after")
    def reversal_link(self) -> Self:
        if self.source == "REVERSAL" and self.reverses is None:
            raise ValueError("a REVERSAL must set reverses")
        if self.source != "REVERSAL" and self.reverses is not None:
            raise ValueError("only a REVERSAL may set reverses")
        return self


class JournalLineOut(BaseModel):
    account_code: str
    account_name: str
    debit: int
    credit: int


class JournalEntryOut(BaseModel):
    """A stored entry. erp_transaction_id is the ERP's own id for it."""

    erp_transaction_id: UUID
    external_reference: str
    entry_date: date
    source: EntrySource
    description: str | None
    reverses: str | None
    total: int = Field(description="Sum of the debits, which equals the sum of the credits.")
    lines: list[JournalLineOut]
    created_at: datetime
    correlation_id: str | None


class JournalPostOut(JournalEntryOut):
    """Answer to a post. CREATED is a new entry; ALREADY_EXISTS is a safe retry."""

    status: Literal["CREATED", "ALREADY_EXISTS"]


class JournalEntryList(BaseModel):
    items: list[JournalEntryOut]


class ReferencePath(BaseModel):
    external_reference: str = Field(min_length=1, max_length=64)


class JournalSearchQuery(BaseModel):
    """Filters for the journal list. reference may repeat, for reconciliation."""

    model_config = ConfigDict(populate_by_name=True)

    date_from: date | None = Field(default=None, alias="from", description="First entry_date.")
    date_to: date | None = Field(default=None, alias="to", description="Last entry_date.")
    reference: list[str] | None = Field(default=None, max_length=1000)
    source: EntrySource | None = None
    q: str | None = Field(
        default=None, max_length=200, description="Text in the reference or description."
    )
    limit: int = Field(default=500, ge=1, le=1000)
