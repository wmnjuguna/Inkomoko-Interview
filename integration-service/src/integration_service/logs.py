"""JSON log lines: one per request, and one per background event, in the same shape.

The brief (section 9) asks for these fields: correlation_id, external_reference, client_uid,
loan_reference, fineract_transaction_id, erp_transaction_id, status, timestamp.
On a request line "status" is the business status when a handler sets one with log_field(),
otherwise the HTTP status. "http_status" always holds the HTTP status."""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from flask import Flask, Response, g, request

CORRELATION_HEADER = "X-Correlation-ID"
logger = logging.getLogger("integration_service")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            line.update(fields)
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False


def new_correlation_id() -> str:
    return f"c-{uuid.uuid4().hex}"


def log_field(name: str, value: Any) -> None:
    """Add a field to this request's log line, e.g. external_reference or status."""
    g.setdefault("log_fields", {})[name] = value


def log_event(
    message: str, *, level: int = logging.INFO, correlation_id: str | None = None, **fields: Any
) -> None:
    """Log one event outside a request, e.g. from background work, in the same JSON shape."""
    logger.log(level, message, extra={"fields": {"correlation_id": correlation_id, **fields}})


def register_request_logging(app: Flask) -> None:
    @app.before_request
    def start() -> None:
        incoming = request.headers.get(CORRELATION_HEADER, "").strip()
        g.correlation_id = incoming[:128] if incoming else new_correlation_id()
        g.started = time.perf_counter()
        g.log_fields = {}
        g.partner = None

    @app.after_request
    def finish(response: Response) -> Response:
        response.headers[CORRELATION_HEADER] = g.get("correlation_id", "")
        fields = {
            "correlation_id": g.get("correlation_id"),
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "http_status": response.status_code,
            "duration_ms": round((time.perf_counter() - g.get("started", 0)) * 1000, 1),
            "partner": g.get("partner"),
            **g.get("log_fields", {}),
        }
        logger.info("request", extra={"fields": fields})
        return response
