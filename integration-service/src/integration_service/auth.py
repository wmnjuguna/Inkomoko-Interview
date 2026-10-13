"""Partner API keys for /api and /admin. /health and /openapi stay open.
The matching partner's name goes into g.partner and into the request log line."""

from __future__ import annotations

import hmac

from flask import Flask, current_app, g, request

from integration_service.errors import ApiError, ErrorCode

API_KEY_HEADER = "X-API-Key"
PROTECTED_PREFIXES = ("/api", "/admin")


def find_partner(sent: str, partner_keys: dict[str, str]) -> str | None:
    """Checks every key, so the time taken does not show which one nearly matched."""
    found = None
    for name, key in partner_keys.items():
        if hmac.compare_digest(sent.encode(), key.encode()):
            found = name
    return found


def register_api_key_check(app: Flask) -> None:
    @app.before_request
    def check_api_key() -> None:
        if not request.path.startswith(PROTECTED_PREFIXES):
            return
        keys: dict[str, str] = current_app.config["PARTNER_API_KEYS"]
        partner = find_partner(request.headers.get(API_KEY_HEADER, ""), keys)
        if partner is None:
            raise ApiError(ErrorCode.UNAUTHENTICATED, "Missing or wrong X-API-Key.", 401)
        g.partner = partner
