"""JSON log lines, one per request, carrying the correlation id."""

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
logger = logging.getLogger("mock_erp")


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
        return json.dumps(line, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False


def log_field(name: str, value: Any) -> None:
    """Add a field to this request's log line, e.g. external_reference or fault."""
    g.setdefault("log_fields", {})[name] = value


def register_request_logging(app: Flask) -> None:
    @app.before_request
    def start() -> None:
        incoming = request.headers.get(CORRELATION_HEADER, "").strip()
        g.correlation_id = incoming[:128] if incoming else f"c-{uuid.uuid4().hex}"
        g.started = time.perf_counter()
        g.log_fields = {}

    @app.after_request
    def finish(response: Response) -> Response:
        response.headers[CORRELATION_HEADER] = g.get("correlation_id", "")
        if not request.path.startswith("/ui/static"):
            fields = {
                "correlation_id": g.get("correlation_id"),
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - g.get("started", 0)) * 1000, 1),
                **g.get("log_fields", {}),
            }
            logger.info("request", extra={"fields": fields})
        return response
