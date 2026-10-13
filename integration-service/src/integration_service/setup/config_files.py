"""Reads the setup inputs from fineract/configuration/*.json.

The folder is FINERACT_SETUP_CONFIG_DIR when set, else fineract/configuration in the repo.
The API user's password never sits in a file: it comes from FINERACT_API_PASSWORD."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from integration_service.setup.product import LoanProductConfig

CONFIG_DIR_ENV = "FINERACT_SETUP_CONFIG_DIR"
API_PASSWORD_ENV = "FINERACT_API_PASSWORD"  # noqa: S105 (an env var name)
REPO_CONFIG_DIR = Path(__file__).resolve().parents[4] / "fineract" / "configuration"


class _Config(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True
    )


class OfficeConfig(_Config):
    name: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=100)
    parent_id: int = Field(gt=0)
    opening_date: date


class CurrencyConfig(_Config):
    code: str = Field(pattern=r"^[A-Z]{3}$")


class RoleConfig(_Config):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    permissions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_repeats(self) -> RoleConfig:
        if len(set(self.permissions)) != len(self.permissions):
            raise ValueError("permissions has a repeated code")
        return self


class ApiUserConfig(_Config):
    username: str = Field(min_length=1, max_length=100)
    firstname: str = Field(min_length=1, max_length=100)
    lastname: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3)
    office: str = Field(min_length=1, description="Office name; must be the office.json one")
    role: str = Field(min_length=1, description="Role name; must be the role.json one")


class SetupConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    office: OfficeConfig
    currency: CurrencyConfig
    role: RoleConfig
    api_user: ApiUserConfig
    loan_product: LoanProductConfig

    @model_validator(mode="after")
    def _files_agree(self) -> SetupConfig:
        if self.api_user.office != self.office.name:
            raise ValueError("user.json office must be the office in office.json")
        if self.api_user.role != self.role.name:
            raise ValueError("user.json role must be the role in role.json")
        if self.loan_product.currency_code != self.currency.code:
            raise ValueError("loan-product.json currencyCode must be the currency.json code")
        return self


def default_config_dir(environ: Mapping[str, str] = os.environ) -> Path:
    raw = environ.get(CONFIG_DIR_ENV, "").strip()
    return Path(raw) if raw else REPO_CONFIG_DIR


def _read(directory: Path, name: str) -> Any:
    with (directory / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_setup_config(directory: Path | None = None) -> SetupConfig:
    """Reads and checks the five setup files."""
    folder = directory or default_config_dir()
    return SetupConfig(
        office=OfficeConfig.model_validate(_read(folder, "office.json")),
        currency=CurrencyConfig.model_validate(_read(folder, "currency.json")),
        role=RoleConfig.model_validate(_read(folder, "role.json")),
        api_user=ApiUserConfig.model_validate(_read(folder, "user.json")),
        loan_product=LoanProductConfig.model_validate(_read(folder, "loan-product.json")),
    )


def api_password_from_env(environ: Mapping[str, str] = os.environ) -> str:
    password = environ.get(API_PASSWORD_ENV, "")
    if not password:
        raise RuntimeError(f"{API_PASSWORD_ENV} is not set")
    return password
