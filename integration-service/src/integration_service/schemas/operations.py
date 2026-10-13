"""Models for partner operations: the external_reference rule, and the status of a request.
They hold our identifiers only, never Fineract's numeric ids."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from integration_service.services.operations import OperationView

EXTERNAL_REFERENCE_PATTERN = r"^[A-Za-z0-9._:\-]{1,40}$"

# The partner's key for one business action: at most 40 letters, digits, . _ : or -.
ExternalReference = Annotated[
    str, StringConstraints(min_length=1, max_length=40, pattern=EXTERNAL_REFERENCE_PATTERN)
]


class TransactionPath(BaseModel):
    external_reference: ExternalReference


class HistoryEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_status: str | None = Field(alias="from")
    to_status: str = Field(alias="to")
    at: datetime
    detail: str | None


class OperationStatus(BaseModel):
    type: str = Field(
        description="CLIENT_CREATION, LOAN_APPLICATION, APPROVAL, DISBURSEMENT or REPAYMENT"
    )
    op_key: str = Field(description="<type>:<external_reference>, e.g. DSB:FIN-1")
    status: str
    erp_status: str | None = Field(
        description="PENDING, SENT or NEEDS_RECONCILIATION "
        "for disbursements and repayments, else null"
    )
    client_uid: str | None
    loan_reference: str | None
    attempts: int
    last_error: str | None
    history: list[HistoryEntry]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: OperationView) -> OperationStatus:
        return cls(
            type=view.type_name,
            op_key=view.op_key,
            status=view.status,
            erp_status=view.erp_status,
            client_uid=view.client_uid,
            loan_reference=view.loan_reference,
            attempts=view.attempts,
            last_error=view.last_error,
            history=[
                HistoryEntry(
                    from_status=e.from_status, to_status=e.to_status, at=e.at, detail=e.detail
                )
                for e in view.history
            ],
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class TransactionResponse(BaseModel):
    external_reference: str
    operations: list[OperationStatus]
