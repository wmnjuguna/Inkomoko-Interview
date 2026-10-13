"""Two columns on operations:
- request_payload: the partner's request as received, so recovery can check a lookup result
  against what was asked (for example the client's name).
- recovery_lookups: how many recovery lookups failed. Kept apart from attempts, which is the
  attempt number in the Fineract Idempotency-Key.

Revision ID: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE operations ADD COLUMN request_payload jsonb, "
        "ADD COLUMN recovery_lookups integer NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE operations DROP COLUMN request_payload, DROP COLUMN recovery_lookups")
