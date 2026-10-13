"""/admin routes. Filled in by the admin milestone."""

from __future__ import annotations

from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

bp = APIBlueprint("admin", __name__, abp_tags=[Tag(name="admin")], abp_security=[{"api_key": []}])
