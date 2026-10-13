"""Table definitions. The schema itself is created by Alembic (migrations/)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Sequence,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

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


# --- Partner operations (migration 0002) ---------------------------------------------------
# Columns named fineract_* hold Fineract's numeric ids: for our own use only, never in a
# public response.

OP_TYPES = ("CLI", "APP", "APR", "DSB", "RPY")
OPERATION_STATUSES = (
    "RECEIVED",
    "FINERACT_PENDING",
    "FINERACT_DONE",
    "ERP_PENDING",
    "COMPLETED",
    "FAILED",
    "NEEDS_RECONCILIATION",
)
ERP_STATUSES = ("PENDING", "SENT", "NEEDS_RECONCILIATION")

loan_reference_seq = Sequence("loan_reference_seq", metadata=metadata)

# One row per partner request, unique per (op_type, external_reference).
operations = Table(
    "operations",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("op_type", Text, nullable=False),
    Column("external_reference", Text, nullable=False),
    Column("op_key", Text, nullable=False, unique=True),
    Column("partner", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("request_payload", JSONB),  # migration 0003
    Column("status", Text, nullable=False),
    Column("client_uid", Text),
    Column("loan_reference", Text),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("recovery_lookups", Integer, nullable=False, server_default="0"),  # migration 0003
    Column("next_attempt_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("response_status", Integer),
    Column("response_body", JSONB),
    Column("fineract_resource_id", BigInteger),
    Column("fineract_transaction_id", BigInteger),
    Column("erp_status", Text),
    Column("erp_transaction_id", Text),
    Column("correlation_id", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("op_type", "external_reference", name="operations_type_reference_key"),
    CheckConstraint(f"op_type IN {OP_TYPES!r}", name="operations_op_type_check"),
    CheckConstraint(f"status IN {OPERATION_STATUSES!r}", name="operations_status_check"),
    CheckConstraint(f"erp_status IN {ERP_STATUSES!r}", name="operations_erp_status_check"),
    Index(
        "operations_one_live_client",
        "client_uid",
        unique=True,
        postgresql_where=text("op_type = 'CLI' AND status <> 'FAILED'"),
    ),
    Index(
        "operations_one_live_approval",
        "loan_reference",
        unique=True,
        postgresql_where=text("op_type = 'APR' AND status <> 'FAILED'"),
    ),
    Index(
        "operations_one_live_disbursement",
        "loan_reference",
        unique=True,
        postgresql_where=text("op_type = 'DSB' AND status <> 'FAILED'"),
    ),
)

# Every status change of an operation: the history shown by GET /api/transactions.
operation_events = Table(
    "operation_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("operation_id", BigInteger, ForeignKey("operations.id"), nullable=False),
    Column("from_status", Text),
    Column("to_status", Text, nullable=False),
    Column("detail", Text),
    Column("correlation_id", Text),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
)

clients = Table(
    "clients",
    metadata,
    Column("client_uid", Text, primary_key=True),
    Column("external_reference", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("first_name", Text),
    Column("last_name", Text),
    Column("fineract_client_id", BigInteger, unique=True),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

loans = Table(
    "loans",
    metadata,
    Column("loan_reference", Text, primary_key=True),
    Column("client_uid", Text, ForeignKey("clients.client_uid"), nullable=False),
    Column("external_reference", Text, nullable=False),
    Column("fineract_loan_id", BigInteger, unique=True),
    Column("principal", BigInteger, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("principal > 0", name="loans_principal_check"),
)

# Journal entries waiting for the ERP. Written in the same transaction that records the
# Fineract result, so a crash can never lose one.
outbox = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("operation_id", BigInteger, ForeignKey("operations.id"), nullable=False, unique=True),
    Column("entry", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default="PENDING"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_error", Text),
    Column("erp_transaction_id", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(f"status IN {ERP_STATUSES!r}", name="outbox_status_check"),
    Index("outbox_status_next_attempt_idx", "status", "next_attempt_at"),
)
