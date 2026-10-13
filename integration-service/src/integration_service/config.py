"""Settings, read once from environment variables."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

from flask import current_app


@dataclass(frozen=True)
class Settings:
    database_url: str
    partner_api_keys: dict[str, str]
    fineract_base_url: str
    fineract_username: str
    fineract_password: str
    erp_base_url: str
    erp_api_key: str
    fineract_tenant: str = "default"
    fineract_timeout_seconds: float = 30.0
    erp_timeout_seconds: float = 10.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=_required("INTEGRATION_DATABASE_URL"),
            partner_api_keys=parse_partner_keys(os.environ.get("PARTNER_API_KEYS", "")),
            fineract_base_url=_required("FINERACT_BASE_URL").rstrip("/"),
            fineract_username=_required("FINERACT_USERNAME"),
            fineract_password=_required("FINERACT_PASSWORD"),
            erp_base_url=_required("ERP_BASE_URL").rstrip("/"),
            erp_api_key=_required("ERP_API_KEY"),
            fineract_tenant=os.environ.get("FINERACT_TENANT", "").strip() or "default",
            fineract_timeout_seconds=env_seconds(os.environ, "FINERACT_TIMEOUT_SECONDS", 30.0),
            erp_timeout_seconds=env_seconds(os.environ, "ERP_TIMEOUT_SECONDS", 10.0),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )


def parse_partner_keys(raw: str) -> dict[str, str]:
    """Reads "partnerA:key1,partnerB:key2". Names and keys must be unique."""
    keys: dict[str, str] = {}
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        name, _, key = (piece.strip() for piece in entry.partition(":"))
        if not name or not key:
            raise RuntimeError("PARTNER_API_KEYS entries must look like name:key")
        if name in keys or key in keys.values():
            raise RuntimeError("PARTNER_API_KEYS has a repeated partner name or key")
        keys[name] = key
    if not keys:
        raise RuntimeError("PARTNER_API_KEYS is not set")
    return keys


def env_seconds(environ: Mapping[str, str], name: str, default: float) -> float:
    """A number of seconds above 0. Blank or unset gives the default (compose passes an
    empty string for a variable missing from .env). RuntimeError names a bad value."""
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a number of seconds above 0, not {raw!r}")
    return value


def get_settings() -> Settings:
    settings: Settings = current_app.extensions["integration_service.settings"]
    return settings


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value
