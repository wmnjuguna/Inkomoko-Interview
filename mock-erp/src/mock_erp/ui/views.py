"""Server-rendered pages under /ui. Filled in by the UI milestone."""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint(
    "ui", __name__, url_prefix="/ui", template_folder="templates", static_folder="static"
)
