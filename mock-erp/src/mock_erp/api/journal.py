"""/journal routes. Filled in by the journal milestone."""

from __future__ import annotations

from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

bp = APIBlueprint(
    "journal", __name__, abp_tags=[Tag(name="journal")], abp_security=[{"api_key": []}]
)
