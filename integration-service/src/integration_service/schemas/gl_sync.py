"""GL sync report. It never holds Fineract's numeric ids."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from integration_service.services.gl_sync import AccountStatus, SyncReport

SyncStatus = Literal["SYNC_PENDING", "SYNCED", "FAILED"]


class AccountSyncItem(BaseModel):
    erp_reference: str = Field(description="The ERP account's external_reference, e.g. GL-1200")
    code: str = Field(description="The ERP account code, also the GL code in Fineract")
    name: str
    status: SyncStatus
    attempts: int = Field(description="How many times it was sent to Fineract")
    last_error: str | None = Field(
        description="Why it is not SYNCED. On a SYNCED account: a warning, e.g. that the "
        "ERP changed it after the sync (updates are out of scope)."
    )


class SyncCounts(BaseModel):
    SYNCED: int
    SYNC_PENDING: int
    FAILED: int


class SyncReportResponse(BaseModel):
    accounts: list[AccountSyncItem]
    counts: SyncCounts
    writes: int = Field(description="Accounts sent to Fineract by this call. 0 for GET.")
    in_progress: bool = Field(
        description="True (HTTP 202) when another sync was already running. This call "
        "changed nothing."
    )

    @classmethod
    def from_report(cls, report: SyncReport) -> SyncReportResponse:
        return cls(
            accounts=[
                AccountSyncItem.model_validate(a, from_attributes=True) for a in report.accounts
            ],
            counts=SyncCounts(**report.counts),
            writes=report.writes,
            in_progress=report.in_progress,
        )


class AccountSyncPath(BaseModel):
    erp_reference: str = Field(description="The ERP account's external_reference, e.g. GL-1200")


class AccountSyncResponse(BaseModel):
    account: AccountSyncItem | None = Field(
        description="The account's state. null only when a sync is running (202) and this "
        "account was never recorded."
    )
    in_progress: bool = Field(
        description="True (HTTP 202) when another sync was already running. This call "
        "changed nothing."
    )

    @classmethod
    def from_status(cls, status: AccountStatus | None, in_progress: bool) -> AccountSyncResponse:
        account = AccountSyncItem.model_validate(status, from_attributes=True) if status else None
        return cls(account=account, in_progress=in_progress)
