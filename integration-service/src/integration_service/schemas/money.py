"""Models for disbursements and repayments. Our identifiers only: loan_reference,
client_uid and external_reference. Money is whole RWF, as Fineract recorded it."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from integration_service.schemas.loans import Rwf
from integration_service.schemas.operations import ExternalReference

PaymentType = Annotated[str, StringConstraints(min_length=1, max_length=50, strip_whitespace=True)]


class DisbursementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_reference: ExternalReference = Field(
        description="Your key for this request. Send the same value on every retry."
    )
    transaction_date: date | None = Field(
        default=None,
        description="YYYY-MM-DD, not in the future (Kigali time). Default: the loan's "
        "expected disbursement date, or today if that is later. The full approved principal "
        "(from Fineract's disbursement template) is paid out.",
    )


class RepaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_reference: ExternalReference = Field(
        description="Your key for this request. Send the same value on every retry."
    )
    amount: Rwf = Field(description="Whole RWF", examples=[100000])
    transaction_date: date | None = Field(
        default=None, description="YYYY-MM-DD, not in the future (Kigali time). Default: today."
    )
    payment_type: PaymentType | None = Field(
        default=None,
        description="Optional. One of the partner payment types Fineract lists, by name "
        "(any case), e.g. Money Transfer. Fineract's system-defined types are refused. "
        "GET /api/loans/{loan_reference}/template?step=repayment lists them.",
    )


class DisbursementResponse(BaseModel):
    """Built from Fineract's own transaction. status and erp_status are as they were when
    this answer was first given; GET /api/transactions/{external_reference} shows the latest."""

    loan_reference: str
    client_uid: str | None
    external_reference: str
    type: str = Field(description="DISBURSEMENT")
    status: str = Field(description="ERP_PENDING, or NEEDS_RECONCILIATION")
    amount: int | None
    transaction_date: date | None
    erp_status: str = Field(description="PENDING or NEEDS_RECONCILIATION")
    loan_status: str | None = Field(description="Fineract's loan status after it, e.g. ACTIVE")


class RepaymentResponse(DisbursementResponse):
    """type is REPAYMENT. The split is Fineract's own."""

    principal_portion: int | None
    interest_portion: int | None
    overpayment_portion: int | None
    fees_portion: int | None
    penalties_portion: int | None
    outstanding_after: int | None = Field(
        description="Loan principal still owed right after this repayment, from Fineract's "
        "loan summary read just after it (null if that read failed)"
    )
