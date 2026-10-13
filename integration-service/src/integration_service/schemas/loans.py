"""Models for /api/loans. Our identifiers only: loan_reference, client_uid and
external_reference. Money is whole RWF."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from integration_service.schemas.clients import ClientUid
from integration_service.schemas.operations import ExternalReference

LOAN_REFERENCE_PATTERN = r"^LN-[0-9]{4}-[0-9]{6}$"
LoanReference = Annotated[str, StringConstraints(pattern=LOAN_REFERENCE_PATTERN)]

# Whole Rwandan francs. A JSON number with a fraction (or a string) is refused.
Rwf = Annotated[StrictInt, Field(gt=0)]


class LoanPath(BaseModel):
    loan_reference: LoanReference


class LoanApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_reference: ExternalReference = Field(
        description="Your key for this request. Send the same value on every retry."
    )
    client_uid: ClientUid = Field(description="A client created through POST /api/clients")
    principal: Rwf = Field(description="Whole RWF, within the product's range", examples=[5000000])
    expected_disbursement_date: date | None = Field(
        default=None,
        description="YYYY-MM-DD, today or later (Kigali time). Default: today. Term, rate and "
        "schedule come from the SME Asset Finance product.",
    )


class LoanApplicationResponse(BaseModel):
    loan_reference: str = Field(description="Our id for the loan, e.g. LN-2026-000001")
    client_uid: str
    principal: int
    status: str = Field(description="SUBMITTED_AND_PENDING_APPROVAL")
    expected_disbursement_date: date
    external_reference: str


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_reference: ExternalReference = Field(
        description="Your key for this request. Send the same value on every retry."
    )
    approved_on: date | None = Field(
        default=None, description="YYYY-MM-DD, not in the future (Kigali time). Default: today."
    )


class ApprovalResponse(BaseModel):
    loan_reference: str
    client_uid: str
    status: str = Field(description="APPROVED")
    approved_on: date
    approved_principal: int
    external_reference: str


class AcceptedResponse(BaseModel):
    """202: the same request is still running. It is not run a second time."""

    external_reference: str
    type: str
    status: str | None
    client_uid: str | None
    loan_reference: str | None
    message: str


class LoanStatus(BaseModel):
    code: str = Field(description="Fineract's status code in our form, e.g. ACTIVE")
    value: str = Field(description="Fineract's own label, e.g. Active")


class LoanBalances(BaseModel):
    principal_disbursed: int | None
    principal_repaid: int | None
    principal_outstanding: int | None
    interest_charged: int | None
    interest_repaid: int | None
    interest_outstanding: int | None
    fees_outstanding: int | None
    penalties_outstanding: int | None
    total_expected_repayment: int | None
    total_repaid: int | None
    total_outstanding: int | None
    total_overdue: int | None
    overpaid_amount: int | None


class LoanResponse(BaseModel):
    """The loan as Fineract reports it right now."""

    loan_reference: str
    client_uid: str
    status: LoanStatus
    closed_obligations_met: bool
    overpaid: bool
    currency: str | None
    principal: int | None
    approved_principal: int | None
    submitted_on: date | None
    approved_on: date | None
    expected_disbursement_on: date | None
    disbursed_on: date | None
    closed_on: date | None
    expected_maturity_on: date | None
    balances: LoanBalances | None = Field(description="null until the loan is disbursed")
    payoff_amount: int | None = Field(
        description="What clears the loan today, from Fineract; only while it is active"
    )
