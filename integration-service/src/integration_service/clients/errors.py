"""Typed failures for calls to Fineract and the ERP.

The main question after a failed call is: did the other side act on it?
outcome_known=True means it surely did not (the request was never sent, or it was refused),
so sending it again is safe. outcome_known=False means it may have worked: look it up
before you send it again.

The names follow the plan (FineractTimeout, ErpRejected, ...), not the Error suffix rule."""

# ruff: noqa: N818

from __future__ import annotations

from typing import Any

import httpx

MAX_IDEMPOTENCY_KEY_LENGTH = 50

# These fail before any byte of the request reaches the server.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def was_not_sent(exc: httpx.TransportError) -> bool:
    """True when the request surely never reached the server."""
    return isinstance(exc, _NOT_SENT)


class InvalidIdempotencyKey(ValueError):
    """The key is empty or longer than Fineract allows (50 characters). Nothing was sent."""


class UpstreamError(Exception):
    """Base for every Fineract and ERP call failure."""

    outcome_known: bool = True

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- Fineract ------------------------------------------------------------------------------


class FineractError(UpstreamError):
    """Any failed Fineract call."""


class FineractUnavailable(FineractError):
    """Could not connect. The request was never sent, so a retry is safe."""


class FineractTimeout(FineractError):
    """Sent, but no full answer came back. It may have worked: look it up before retrying."""

    outcome_known = False


class FineractRejected(FineractError):
    """Fineract answered 4xx. Nothing was written. Holds Fineract's parsed error body."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        self.developer_message = str(body.get("developerMessage") or "")
        self.error_codes = fineract_error_codes(body)
        super().__init__(
            f"Fineract answered {status}: {', '.join(self.error_codes) or self.developer_message}"
        )


class FineractConflict(FineractRejected):
    """409: the data clashes with what Fineract already holds."""


class FineractNotFound(FineractRejected):
    """404: Fineract does not know this resource."""


class FineractServerError(FineractError):
    """Fineract answered 5xx, or an answer we could not read. It may have worked."""

    outcome_known = False

    def __init__(self, status: int, body: dict[str, Any], message: str | None = None) -> None:
        self.status = status
        self.body = body
        super().__init__(message or f"Fineract answered {status}")


def fineract_error_codes(body: dict[str, Any]) -> list[str]:
    """userMessageGlobalisationCode from the top level and from each error, in order."""
    codes: list[str] = []
    top = body.get("userMessageGlobalisationCode")
    if isinstance(top, str):
        codes.append(top)
    errors = body.get("errors")
    if isinstance(errors, list):
        for item in errors:
            code = item.get("userMessageGlobalisationCode") if isinstance(item, dict) else None
            if isinstance(code, str) and code not in codes:
                codes.append(code)
    return codes


# --- ERP -----------------------------------------------------------------------------------


class ErpError(UpstreamError):
    """Any failed ERP call."""


class ErpUnavailable(ErpError):
    """Could not connect, or the ERP answered 503. Nothing was saved; a retry is safe."""


class ErpTimeout(ErpError):
    """Sent, but no answer (or a 504). It may have been saved: look it up before retrying."""

    outcome_known = False


class ErpServerError(ErpError):
    """The ERP answered another 5xx, or an answer we could not read. It may have been saved."""

    outcome_known = False

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class ErpRateLimited(ErpError):
    """429. Nothing was saved. retry_after is the wait in seconds, when the ERP gave one."""

    def __init__(self, retry_after: int | None) -> None:
        self.retry_after = retry_after
        super().__init__(f"ERP rate limit, retry after {retry_after} seconds")


class ErpRejected(ErpError):
    """The ERP refused the request (4xx). Holds its error body: code, message, details."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        error = body.get("error")
        error = error if isinstance(error, dict) else {}
        self.code = str(error.get("code") or "")
        self.details: dict[str, Any] = error.get("details") or {}
        super().__init__(str(error.get("message") or f"ERP answered {status}"))


class ErpConflict(ErpRejected):
    """409: the reference (or code) is already stored with different content."""
