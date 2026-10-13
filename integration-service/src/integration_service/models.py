"""Table definitions. The schema itself is created by Alembic (migrations/)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    func,
)

metadata = MetaData()

GL_SYNC_STATUSES = ("SYNC_PENDING", "SYNCED", "FAILED")

# One row per ERP account we sync to Fineract, keyed by the ERP external_reference.
# fineract_gl_id is for our own use only. It never appears in a public response.
gl_sync = Table(
    "gl_sync",
    metadata,
    Column("erp_reference", Text, primary_key=True),
    Column("erp_code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("erp_type", Text, nullable=False),
    Column("erp_usage", Text, nullable=False),
    Column("erp_parent_reference", Text),
    Column("payload_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("fineract_gl_id", BigInteger),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("last_attempt_at", DateTime(timezone=True)),
    Column("synced_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(f"status IN {GL_SYNC_STATUSES!r}", name="gl_sync_status_check"),
)
