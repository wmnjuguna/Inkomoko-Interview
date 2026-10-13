"""Models for the fault switch at /admin/faults."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FaultMode = Literal["down", "timeout", "timeout_after_write", "rate_limited"]


class FaultSettingIn(BaseModel):
    """Turn a fault on. Only paths starting with path_prefix fail."""

    model_config = ConfigDict(extra="forbid")

    mode: FaultMode
    path_prefix: str = Field(default="/api", description="Must start with /api.")
    delay_seconds: int | None = Field(
        default=None,
        ge=1,
        le=120,
        description="Wait for the timeout modes. Default: the ERP_TIMEOUT_DELAY setting.",
    )
    remaining: int | None = Field(
        default=None, ge=1, description="How many calls fail. Null means until cleared."
    )
    retry_after: int = Field(
        default=5, ge=1, le=3600, description="Retry-After seconds for rate_limited."
    )

    @field_validator("path_prefix")
    @classmethod
    def prefix_under_api(cls, value: str) -> str:
        if not value.startswith("/api"):
            raise ValueError("must start with /api")
        return value


class FaultSettingOut(BaseModel):
    """The current fault setting. mode is null when no fault is on."""

    mode: FaultMode | None
    path_prefix: str
    delay_seconds: int
    remaining: int | None
    retry_after: int
    active: bool
