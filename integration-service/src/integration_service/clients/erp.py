"""ERP client: X-API-Key, JSON, and typed errors.

The ERP is idempotent by external_reference, so a journal entry posted twice with the
same reference and content is saved once."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from integration_service.clients.errors import (
    ErpConflict,
    ErpRateLimited,
    ErpRejected,
    ErpServerError,
    ErpTimeout,
    ErpUnavailable,
    was_not_sent,
)

logger = logging.getLogger("integration_service.clients.erp")

ErpAccountType = Literal["ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"]
ErpAccountUsage = Literal["HEADER", "DETAIL"]
JournalSource = Literal["DISBURSEMENT", "REPAYMENT", "REVERSAL", "MANUAL"]
QueryParams = list[tuple[str, str | int | float | bool | None]]


class ErpAccount(BaseModel):
    """One account in the ERP chart of accounts."""

    model_config = ConfigDict(frozen=True)

    code: str
    external_reference: str
    name: str
    type: ErpAccountType
    usage: ErpAccountUsage
    parent_code: str | None
    currency: str
    created_at: datetime


class _AccountList(BaseModel):
    items: list[ErpAccount]


class ErpClient:
    """Calls the ERP's REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def list_accounts(self, correlation_id: str | None = None) -> list[ErpAccount]:
        """The whole chart, each HEADER before its children."""
        data = self._call("GET", "/api/accounts", correlation_id=correlation_id)
        try:
            return _AccountList.model_validate(data).items
        except ValidationError as exc:
            raise ErpServerError(200, f"Unexpected ERP answer: {exc}") from exc

    def post_journal_entry(
        self, entry: dict[str, Any], correlation_id: str | None = None
    ) -> dict[str, Any]:
        """Save an entry. 201 CREATED or 200 ALREADY_EXISTS both come back as the body."""
        return _as_dict(
            self._call("POST", "/api/journal-entries", body=entry, correlation_id=correlation_id)
        )

    def get_journal_entry(
        self, external_reference: str, correlation_id: str | None = None
    ) -> dict[str, Any] | None:
        """One stored entry, or None when the ERP has no entry with this reference."""
        path = "/api/journal-entries/" + quote(external_reference, safe="")
        try:
            return _as_dict(self._call("GET", path, correlation_id=correlation_id))
        except ErpRejected as exc:
            if exc.status == 404:
                return None
            raise

    def search_journal_entries(
        self,
        references: list[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        source: JournalSource | None = None,
        q: str | None = None,
        limit: int | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Entries matching every filter given, newest first."""
        params: QueryParams = [("reference", ref) for ref in references or []]
        optional = {
            "from": date_from.isoformat() if date_from else None,
            "to": date_to.isoformat() if date_to else None,
            "source": source,
            "q": q,
            "limit": str(limit) if limit is not None else None,
        }
        params += [(name, value) for name, value in optional.items() if value is not None]
        data = self._call(
            "GET", "/api/journal-entries", params=params, correlation_id=correlation_id
        )
        items = _as_dict(data).get("items")
        if not isinstance(items, list):
            raise ErpServerError(200, "Unexpected ERP answer: no items list")
        return items

    def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: QueryParams | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        headers = {"X-Correlation-ID": correlation_id} if correlation_id else {}
        started = time.monotonic()
        try:
            response = self._http.request(
                method,
                path,
                json=body if method != "GET" else None,
                params=params or None,
                headers=headers,
            )
        except httpx.TransportError as exc:
            _log(method, path, None, started, correlation_id, type(exc).__name__)
            if was_not_sent(exc):
                raise ErpUnavailable(f"ERP not reachable: {exc}") from exc
            raise ErpTimeout(f"No answer from the ERP: {exc}") from exc
        _log(method, path, response.status_code, started, correlation_id)
        return _result(response)


def _result(response: httpx.Response) -> Any:
    status = response.status_code
    try:
        data = response.json() if response.content else None
    except ValueError:
        data = None
    if status < 400:
        return data
    body = data if isinstance(data, dict) else {}
    if status == 409:
        raise ErpConflict(status, body)
    if status == 429:
        raise ErpRateLimited(_retry_after(response.headers.get("Retry-After")))
    if status < 500:
        raise ErpRejected(status, body)
    if status == 503:
        raise ErpUnavailable("The ERP answered 503")
    if status == 504:
        raise ErpTimeout("The ERP answered 504")
    raise ErpServerError(status, f"The ERP answered {status}")


def _retry_after(value: str | None) -> int | None:
    if value is None or not value.strip().isdigit():
        return None
    return int(value.strip())


def _as_dict(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ErpServerError(200, "Unexpected ERP answer: not a JSON object")
    return data


def _log(
    method: str,
    path: str,
    status: int | None,
    started: float,
    correlation_id: str | None,
    failure: str | None = None,
) -> None:
    fields = {
        "upstream": "erp",
        "method": method,
        "path": path,
        "http_status": status,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "correlation_id": correlation_id,
        "failure": failure,
    }
    logger.debug("erp %s %s -> %s", method, path, status or failure, extra={"fields": fields})
