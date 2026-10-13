"""App factory. Gunicorn runs mock_erp.app:create_app()."""

from __future__ import annotations

from typing import Any

from flask_openapi3.models.info import Info
from flask_openapi3.models.security_scheme import SecurityScheme
from flask_openapi3.openapi import OpenAPI
from sqlalchemy import Engine

from mock_erp import auth, faults, logs
from mock_erp.api import accounts as accounts_api
from mock_erp.api import admin as admin_api
from mock_erp.api import health as health_api
from mock_erp.api import journal as journal_api
from mock_erp.config import Settings
from mock_erp.db import make_engine
from mock_erp.errors import register_error_handlers, validation_error_response
from mock_erp.schemas.common import ErrorResponse
from mock_erp.ui import views as ui_views

SECURITY_SCHEMES: dict[str, SecurityScheme | dict[str, Any]] = {
    "api_key": {"type": "apiKey", "in": "header", "name": auth.API_KEY_HEADER}
}


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> OpenAPI:
    settings = settings or Settings.from_env()
    logs.configure_logging(settings.log_level)

    app = OpenAPI(
        __name__,
        info=Info(
            title="Mock ERP",
            version="0.1.0",
            description="Chart of accounts and journal for the Inkomoko take-home. "
            "Refuses unbalanced entries and duplicates on its own, and can fail on purpose.",
        ),
        security_schemes=SECURITY_SCHEMES,
        validation_error_status=422,
        validation_error_callback=validation_error_response,
        validation_error_model=ErrorResponse,
        doc_prefix="/openapi",
    )
    app.config["ERP_API_KEY"] = settings.api_key
    app.config["ERP_TIMEOUT_DELAY"] = settings.timeout_delay
    app.extensions["mock_erp.engine"] = engine or make_engine(settings.database_url)

    # Order matters: correlation id first, then the key check, then the fault switch.
    logs.register_request_logging(app)
    auth.register_api_key_check(app)
    faults.register_fault_hooks(app)
    register_error_handlers(app)

    app.register_api(health_api.bp)
    app.register_api(accounts_api.bp)
    app.register_api(journal_api.bp)
    app.register_api(admin_api.bp)
    app.register_blueprint(ui_views.bp)
    return app
