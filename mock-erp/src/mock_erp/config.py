"""Settings, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_key: str
    log_level: str = "INFO"
    timeout_delay: float = 10.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=_required("ERP_DATABASE_URL"),
            api_key=_required("ERP_API_KEY"),
            log_level=os.environ.get("ERP_LOG_LEVEL", "INFO").upper(),
            timeout_delay=float(os.environ.get("ERP_TIMEOUT_DELAY", "10")),
        )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value
