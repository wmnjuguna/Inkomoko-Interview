"""API key check for /api and /admin. /health, /openapi and /ui stay open (local only)."""

from __future__ import annotations

import hmac

from flask import Flask, current_app, request

from mock_erp.errors import ErpError, ErrorCode

API_KEY_HEADER = "X-API-Key"
PROTECTED_PREFIXES = ("/api", "/admin")


def register_api_key_check(app: Flask) -> None:
    @app.before_request
    def check_api_key() -> None:
        if not request.path.startswith(PROTECTED_PREFIXES):
            return
        sent = request.headers.get(API_KEY_HEADER, "")
        expected: str = current_app.config["ERP_API_KEY"]
        if not hmac.compare_digest(sent.encode(), expected.encode()):
            raise ErpError(ErrorCode.UNAUTHORIZED, "Missing or wrong X-API-Key.", 401)
