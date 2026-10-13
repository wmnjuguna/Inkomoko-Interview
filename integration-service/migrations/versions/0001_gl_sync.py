"""Schema: gl_sync, one row per ERP account synced to Fineract.

Revision ID: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# fineract_gl_id is for our own use only. It never appears in a public response.
TABLES = """
CREATE TABLE gl_sync (
    erp_reference        text PRIMARY KEY,
    erp_code             text NOT NULL UNIQUE,
    name                 text NOT NULL,
    erp_type             text NOT NULL,
    erp_usage            text NOT NULL,
    erp_parent_reference text,
    payload_hash         text NOT NULL,
    status               text NOT NULL
        CONSTRAINT gl_sync_status_check CHECK (status IN ('SYNC_PENDING', 'SYNCED', 'FAILED')),
    fineract_gl_id       bigint,
    attempts             integer NOT NULL DEFAULT 0,
    last_error           text,
    last_attempt_at      timestamptz,
    synced_at            timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute("DROP TABLE gl_sync")
