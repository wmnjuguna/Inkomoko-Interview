"""App factory. Gunicorn runs integration_service.app:create_app()."""

from __future__ import annotations

from typing import Any

from flask_openapi3.models.info import Info
from flask_openapi3.models.security_scheme import SecurityScheme
from flask_openapi3.openapi import OpenAPI
from sqlalchemy import Engine

from integration_service import auth, logs
from integration_service.api import gl_sync as gl_sync_api
from integration_service.api import health as health_api
from integration_service.config import Settings
from integration_service.db import make_engine
from integration_service.errors import register_error_handlers, validation_error_response
from integration_service.schemas.common import ErrorResponse

SECURITY_SCHEMES: dict[str, SecurityScheme | dict[str, Any]] = {
    "api_key": {"type": "apiKey", "in": "header", "name": auth.API_KEY_HEADER}
}


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> OpenAPI:
    settings = settings or Settings.from_env()
    logs.configure_logging(settings.log_level)

    app = OpenAPI(
        __name__,
        info=Info(
            title="Integration Service",
            version="0.1.0",
            description="The only API partners call. Syncs the ERP chart of accounts to "
            "Fineract, and runs clients, loans and payments through Fineract and the ERP.",
        ),
        security_schemes=SECURITY_SCHEMES,
        validation_error_status=422,
        validation_error_callback=validation_error_response,
        validation_error_model=ErrorResponse,
        doc_prefix="/openapi",
    )
    app.config["PARTNER_API_KEYS"] = dict(settings.partner_api_keys)
    app.extensions["integration_service.settings"] = settings
    app.extensions["integration_service.engine"] = engine or make_engine(settings.database_url)

    # Order matters: correlation id first, then the key check.
    logs.register_request_logging(app)
    auth.register_api_key_check(app)
    register_error_handlers(app)

    app.register_api(health_api.bp)
    app.register_api(gl_sync_api.bp)
    return app
