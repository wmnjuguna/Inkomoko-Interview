"""Request and response models for the chart of accounts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

AccountType = Literal["ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"]
AccountUsage = Literal["HEADER", "DETAIL"]
NormalSide = Literal["DEBIT", "CREDIT"]
CreateStatus = Literal["CREATED", "ALREADY_EXISTS"]

AccountCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{4,10}$")]
ExternalReference = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.\-]*$"),
]
AccountName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CreateAccountRequest(BaseModel):
    """A new account. Posting the same body again is safe."""

    model_config = ConfigDict(extra="forbid")

    code: AccountCode = Field(description="Digits only, 4 to 10 long, e.g. 1400.")
    external_reference: ExternalReference = Field(description="Caller's id, e.g. GL-1400.")
    name: AccountName
    type: AccountType
    usage: AccountUsage = Field(description="HEADER groups accounts; only DETAIL takes lines.")
    parent_code: AccountCode | None = Field(
        default=None, description="A HEADER of the same type. Required for DETAIL accounts."
    )
    currency: Literal["RWF"] = "RWF"


class Account(BaseModel):
    """An account as stored."""

    code: str
    external_reference: str
    name: str
    type: AccountType
    usage: AccountUsage
    parent_code: str | None
    currency: str
    created_at: datetime


class AccountCreated(Account):
    """The stored account, and whether this call created it."""

    status: CreateStatus


class AccountList(BaseModel):
    items: list[Account]


class AccountListQuery(BaseModel):
    usage: AccountUsage | None = None


class AccountPath(BaseModel):
    code: str


class AccountBalance(BaseModel):
    """Totals in whole RWF. A HEADER adds up every account under it."""

    code: str
    name: str
    type: AccountType
    usage: AccountUsage
    debit_total: int
    credit_total: int
    balance: int
    normal_side: NormalSide
