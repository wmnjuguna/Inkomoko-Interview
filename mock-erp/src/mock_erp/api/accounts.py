"""/accounts routes. Filled in by the accounts milestone."""

from __future__ import annotations

from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

bp = APIBlueprint(
    "accounts", __name__, abp_tags=[Tag(name="accounts")], abp_security=[{"api_key": []}]
)
