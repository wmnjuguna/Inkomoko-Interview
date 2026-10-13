"""Fault switch: lets the ERP fail on purpose (down, timeout, timeout after write, 429).

The setting is one database row, so every Gunicorn worker sees the same switch.
"""

from __future__ import annotations

from flask import Flask


def register_fault_hooks(app: Flask) -> None:
    """Placeholder: the fault hooks are added in the faults milestone."""
