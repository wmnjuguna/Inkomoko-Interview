"""Models for the form endpoints: GET /api/clients/template, GET /api/loans/template and
GET /api/loans/{loan_reference}/template. Defaults, ranges and options come from Fineract's
own templates. Our identifiers only, never Fineract ids."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from integration_service.schemas.clients import ClientUid
from integration_service.services.templates import Step

# A value of a form field: a whole number, a decimal number, or a string (a date is
# YYYY-MM-DD).
FieldValue = int | float | str | None


class FormOption(BaseModel):
    value: str = Field(description="What to send")
    label: str = Field(description="Fineract's label for it")


class FormField(BaseModel):
    name: str = Field(description="The JSON name in the request body")
    type: Literal["string", "integer", "number", "date"]
    required: bool
    read_only: bool = Field(
        description="True: shown for information, set by Fineract or the product; do not send it"
    )
    default: FieldValue = Field(description="What Fineract proposes, or what is used if left out")
    minimum: FieldValue
    maximum: FieldValue
    multiple_of: int | None
    pattern: str | None
    max_length: int | None
    options: list[FormOption] | None = Field(description="The allowed values, when limited")
    description: str


class CurrencyInfo(BaseModel):
    code: str
    name: str
    decimal_places: int
    in_multiples_of: int


class ProductInfo(BaseModel):
    name: str
    short_name: str | None
    description: str | None
    currency: CurrencyInfo


class ClientFormResponse(BaseModel):
    form: str = Field(description="client")
    submit_to: str
    office: str | None = Field(description="The Fineract office new clients go to")
    activation_date: str | None = Field(description="Fineract's default: today, YYYY-MM-DD")
    fields: list[FormField]


class LoanFormQuery(BaseModel):
    client_uid: ClientUid | None = Field(
        default=None,
        description="Optional: a client created through POST /api/clients. The form then "
        "carries that client's defaults.",
    )


class LoanApplicationFormResponse(BaseModel):
    form: str = Field(description="loan_application")
    submit_to: str
    client_uid: str | None
    product: ProductInfo
    fields: list[FormField]


class StepQuery(BaseModel):
    step: Step = Field(description="approval, disbursement, repayment or payoff")


class StepFormResponse(BaseModel):
    form: str = Field(description="The step: approval, disbursement, repayment or payoff")
    loan_reference: str
    client_uid: str | None
    loan_status: str = Field(description="Fineract's loan status now, e.g. APPROVED")
    available: bool = Field(description="True when the loan's status allows this step now")
    message: str | None = Field(description="Why the step is not available, else null")
    submit_to: str
    currency: CurrencyInfo
    fields: list[FormField]
    values: dict[str, FieldValue] = Field(
        description="What Fineract reports for this step, e.g. the next installment or the "
        "payoff amount and its split"
    )
