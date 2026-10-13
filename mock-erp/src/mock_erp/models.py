"""Table definitions. The schema itself is created by Alembic (migrations/)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    func,
)

metadata = MetaData()

ACCOUNT_TYPES = ("ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE")
ACCOUNT_USAGES = ("HEADER", "DETAIL")
ENTRY_SOURCES = ("DISBURSEMENT", "REPAYMENT", "REVERSAL", "MANUAL")
FAULT_MODES = ("down", "timeout", "timeout_after_write", "rate_limited")

accounts = Table(
    "accounts",
    metadata,
    Column("code", Text, primary_key=True),
    Column("external_reference", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("usage", Text, nullable=False),
    Column("parent_code", Text, ForeignKey("accounts.code")),
    Column("currency", Text, nullable=False, server_default="RWF"),
    Column("request_hash", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(f"type IN {ACCOUNT_TYPES!r}", name="accounts_type_check"),
    CheckConstraint(f"usage IN {ACCOUNT_USAGES!r}", name="accounts_usage_check"),
)

journal_entries = Table(
    "journal_entries",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("external_reference", Text, nullable=False, unique=True),
    Column("request_hash", Text, nullable=False),
    Column("entry_date", Date, nullable=False),
    Column("source", Text, nullable=False),
    Column("reverses", Text, ForeignKey("journal_entries.external_reference")),
    Column("description", Text),
    Column("correlation_id", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(f"source IN {ENTRY_SOURCES!r}", name="journal_entries_source_check"),
)

journal_lines = Table(
    "journal_lines",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("entry_id", Uuid, ForeignKey("journal_entries.id"), nullable=False),
    Column("account_code", Text, ForeignKey("accounts.code"), nullable=False),
    Column("debit", BigInteger, nullable=False, server_default="0"),
    Column("credit", BigInteger, nullable=False, server_default="0"),
    CheckConstraint(
        "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
        name="journal_lines_one_side_check",
    ),
)

fault_settings = Table(
    "fault_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("mode", Text),
    Column("path_prefix", Text, nullable=False, server_default="/api"),
    Column("delay_seconds", Integer, nullable=False, server_default="10"),
    Column("remaining", Integer),
    Column("retry_after", Integer, nullable=False, server_default="5"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("id = 1", name="fault_settings_single_row"),
    CheckConstraint(f"mode IS NULL OR mode IN {FAULT_MODES!r}", name="fault_settings_mode_check"),
)
