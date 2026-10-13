"""Models for /api/clients. Our identifiers only: client_uid and external_reference."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from integration_service.schemas.operations import ExternalReference

# A business prefix of 2 to 10 capital letters, a dash and 6 digits, e.g. SME-000001.
CLIENT_UID_PATTERN = r"^[A-Z]{2,10}-[0-9]{6}$"
ClientUid = Annotated[str, StringConstraints(pattern=CLIENT_UID_PATTERN)]

# Letters (any language), joined by single spaces, apostrophes, dots or hyphens.
# Fineract allows 50 characters for each name.
PersonName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=50,
        pattern=r"^\p{L}+(?:[ '.\-]\p{L}+)*\.?$",
    ),
]


class CreateClientRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_reference: ExternalReference = Field(
        description="Your key for this request. Send the same value on every retry."
    )
    client_uid: ClientUid = Field(
        description="Your business id for the client, e.g. SME-000001. Unique.",
        examples=["SME-000001"],
    )
    first_name: PersonName
    last_name: PersonName

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class ClientResponse(BaseModel):
    client_uid: str
    name: str
    status: str = Field(description="ACTIVE: the client is active from creation")
    external_reference: str


class ClientPath(BaseModel):
    client_uid: ClientUid


class ClientQuery(BaseModel):
    name: str | None = Field(
        default=None,
        max_length=101,
        description="Optional: the name you expect. name_matches says whether Fineract agrees "
        "(case and spacing ignored).",
    )


class ClientCheckResponse(BaseModel):
    client_uid: str
    name: str = Field(description="The name Fineract holds")
    active: bool
    status: str = Field(description="Fineract's client status, e.g. ACTIVE, PENDING, CLOSED")
    matches_our_record: bool = Field(
        description="True when we created this client, Fineract holds the same name, "
        "and the client is active"
    )
    name_matches: bool | None = Field(description="Only when you sent ?name=, else null")
