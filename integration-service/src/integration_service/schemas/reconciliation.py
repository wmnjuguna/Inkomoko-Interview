"""Models for POST /api/reconciliation. Items carry our references only (op keys, loan
references), never a Fineract numeric id. Amounts are whole RWF."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_RANGE_DAYS = 31
MAX_REFERENCES = 100

# An op key (DSB:FIN-1, RPY:FIN-1:REV), a partner reference (FIN-1) or a foreign externalId.
ReconReference = Annotated[
    str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:\-]+$")
]


class ReconciliationRequest(BaseModel):
    """Either a date range (from and to, at most 31 days) or a list of references."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    date_from: date | None = Field(
        default=None, alias="from", description="First date (Fineract and ERP entry date)."
    )
    date_to: date | None = Field(default=None, alias="to", description="Last date, inclusive.")
    references: list[ReconReference] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_REFERENCES,
        description="Op keys (DSB:FIN-1), partner references (FIN-1: both DSB: and RPY:) or "
        "foreign Fineract externalIds. Each also selects its :REV reversal.",
    )


class Line(BaseModel):
    account_code: str
    debit: int
    credit: int


class FineractSide(BaseModel):
    type: str = Field(description="DISBURSEMENT or REPAYMENT")
    transaction_date: date
    amount: int
    principal_portion: int
    interest_portion: int
    overpayment_portion: int
    fees_portion: int
    penalties_portion: int
    reversed: bool


class ErpSide(BaseModel):
    entry_date: date
    source: str
    reverses: str | None
    total: int
    lines: list[Line]


class OurSide(BaseModel):
    status: str = Field(description="Our operation status")
    erp_status: str | None = Field(description="Our outbox status: PENDING, SENT, ...")


class Item(BaseModel):
    key: str | None = Field(
        description="The matching key: Fineract externalId = ERP external_reference. "
        "null for a Fineract transaction without externalId."
    )
    outcome: str = Field(
        description="MATCHED, MISSING_IN_ERP, MISSING_IN_FINERACT, AMOUNT_MISMATCH or IN_FLIGHT"
    )
    loan_reference: str | None
    type: str = Field(description="DISBURSEMENT, REPAYMENT or REVERSAL")
    fineract: FineractSide | None
    erp: ErpSide | None
    expected_lines: list[Line] | None = Field(
        description="The lines our Part H mapping builds from Fineract's own transaction"
    )
    ours: OurSide | None
    reasons: list[str]


class Summary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    matched: int = Field(alias="MATCHED")
    missing_in_erp: int = Field(alias="MISSING_IN_ERP")
    missing_in_fineract: int = Field(alias="MISSING_IN_FINERACT")
    amount_mismatch: int = Field(alias="AMOUNT_MISMATCH")
    in_flight: int = Field(alias="IN_FLIGHT")
    total: int


class NotCompared(BaseModel):
    fineract_types: dict[str, int] = Field(
        description="Fineract transactions skipped, by type (accruals and other non-cash types)"
    )
    erp_entries: int = Field(description="ERP entries whose reference is not an op key")


class Scope(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date_from: date | None = Field(alias="from")
    date_to: date | None = Field(alias="to")
    references: list[str] | None


class ReconciliationResponse(BaseModel):
    scope: Scope
    generated_at: datetime
    compared_types: list[str]
    summary: Summary
    not_compared: NotCompared
    warnings: list[str]
    items: list[Item]
