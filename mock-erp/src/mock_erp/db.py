"""Database engine. Services open their own short transactions with engine.begin()."""

from __future__ import annotations

from flask import current_app
from sqlalchemy import Engine, create_engine


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def get_engine() -> Engine:
    engine: Engine = current_app.extensions["mock_erp.engine"]
    return engine
