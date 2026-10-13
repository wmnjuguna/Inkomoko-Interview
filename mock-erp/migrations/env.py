"""Alembic runs online only. The URL comes from ERP_DATABASE_URL, or from
config.attributes["connection"] when tests run migrations themselves."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from mock_erp.models import metadata

config = context.config


def run() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(os.environ["ERP_DATABASE_URL"])
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()


run()
