"""Fineract client: basic auth, tenant header, JSON, and typed errors.

Every write can carry an Idempotency-Key. Fineract keeps the first answer for that key
and sends it again on a repeat, so a retry with the same key never writes twice."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from integration_service.clients.errors import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    FineractConflict,
    FineractNotFound,
    FineractRejected,
    FineractServerError,
    FineractTimeout,
    FineractUnavailable,
    InvalidIdempotencyKey,
    was_not_sent,
)

logger = logging.getLogger("integration_service.clients.fineract")

API_SUFFIX = "/api/v1"
ERP_ACCOUNTS_PATH = "/erp-integration/accounts/"


class ErpAccountSyncResult(BaseModel):
    """The plugin's answer to a sync: which GL account belongs to the ERP reference."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    erp_reference: str = Field(alias="erpReference")
    gl_account_id: int = Field(alias="glAccountId")
    gl_code: str = Field(alias="glCode")
    outcome: Literal["CREATED", "UNCHANGED"]


class ErpAccountMapping(BaseModel):
    """A synced GL account, as the plugin's GET shows it."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    erp_reference: str = Field(alias="erpReference")
    gl_account_id: int = Field(alias="glAccountId")
    gl_code: str = Field(alias="glCode")
    name: str
    type: str
    usage: str
    parent_account: str | None = Field(default=None, alias="parentAccount")
    hierarchy: str | None = None
    synced_at: datetime | None = Field(default=None, alias="syncedAt")


def check_idempotency_key(key: str | None) -> None:
    if key is not None and not 1 <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidIdempotencyKey(
            f"Idempotency-Key must be 1 to {MAX_IDEMPOTENCY_KEY_LENGTH} characters, got {len(key)}"
        )


class FineractClient:
    """Calls Fineract's REST API. base_url ends in /fineract-provider/api/v1."""

    def __init__(
        self,
        base_url: str,
        tenant: str,
        username: str,
        password: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        root = self.base_url.removesuffix(API_SUFFIX)
        self.health_url = f"{root}/actuator/health"
        self._http = httpx.Client(
            base_url=self.base_url,
            auth=(username, password),
            headers={"Fineract-Platform-TenantId": tenant, "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    # --- generic helpers -------------------------------------------------------------------

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        return self._call("GET", path, params=params, correlation_id=correlation_id)

    def post(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        return self._call(
            "POST",
            path,
            body=body,
            params=params,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

    def put(
        self,
        path: str,
        body: Any,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        return self._call(
            "PUT",
            path,
            body=body,
            params=params,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

    # --- ERP account sync (the plugin) -----------------------------------------------------

    def put_erp_account(
        self,
        erp_reference: str,
        body: dict[str, Any],
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> ErpAccountSyncResult:
        """Create the GL account for an ERP account, or confirm it is already there."""
        data = self.put(
            _erp_account_path(erp_reference),
            body,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
        return _parse(ErpAccountSyncResult, data)

    def get_erp_account(
        self, erp_reference: str, correlation_id: str | None = None
    ) -> ErpAccountMapping | None:
        """The GL account synced for this ERP reference, or None if there is none yet."""
        try:
            data = self.get(_erp_account_path(erp_reference), correlation_id=correlation_id)
        except FineractNotFound:
            return None
        return _parse(ErpAccountMapping, data)

    # --- health ----------------------------------------------------------------------------

    def health(self) -> bool:
        """True when Fineract's actuator says UP."""
        try:
            response = self._http.get(self.health_url)
        except httpx.TransportError as exc:
            logger.debug("fineract health unreachable: %s", type(exc).__name__)
            return False
        if response.status_code != 200:
            return False
        try:
            return bool(response.json().get("status") == "UP")
        except (ValueError, AttributeError):
            return False

    # --- internals -------------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        check_idempotency_key(idempotency_key)
        headers: dict[str, str] = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if correlation_id is not None:
            headers["X-Correlation-ID"] = correlation_id
        started = time.monotonic()
        try:
            response = self._http.request(
                method,
                path,
                json=body if method != "GET" else None,
                params=params,
                headers=headers,
            )
        except httpx.TransportError as exc:
            _log(method, path, None, started, idempotency_key, correlation_id, type(exc).__name__)
            if was_not_sent(exc):
                raise FineractUnavailable(f"Fineract not reachable: {exc}") from exc
            raise FineractTimeout(f"No answer from Fineract: {exc}") from exc
        _log(method, path, response.status_code, started, idempotency_key, correlation_id)
        return _result(response)


def _erp_account_path(erp_reference: str) -> str:
    return ERP_ACCOUNTS_PATH + quote(erp_reference, safe="")


def _json_or_empty(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _result(response: httpx.Response) -> Any:
    status = response.status_code
    data = _json_or_empty(response)
    if status < 400:
        return data
    body = data if isinstance(data, dict) else {}
    if status == 404:
        raise FineractNotFound(status, body)
    if status == 409:
        raise FineractConflict(status, body)
    if status < 500:
        raise FineractRejected(status, body)
    raise FineractServerError(status, body)


def _parse[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # The details quote the body (with Fineract ids), so they go to the log only.
        logger.warning(
            "unexpected fineract answer for %s: %s",
            model.__name__,
            exc.errors(include_input=False, include_url=False),
        )
        body = data if isinstance(data, dict) else {}
        raise FineractServerError(
            200, body, "Unexpected Fineract answer (unreadable body)"
        ) from exc


def _log(
    method: str,
    path: str,
    status: int | None,
    started: float,
    idempotency_key: str | None,
    correlation_id: str | None,
    failure: str | None = None,
) -> None:
    fields = {
        "upstream": "fineract",
        "method": method,
        "path": path,
        "http_status": status,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "idempotency_key": idempotency_key,
        "correlation_id": correlation_id,
        "failure": failure,
    }
    logger.debug("fineract %s %s -> %s", method, path, status or failure, extra={"fields": fields})
