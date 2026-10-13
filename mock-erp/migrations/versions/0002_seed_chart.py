"""Seed the two-level chart of accounts. Safe to re-run: existing codes are left alone.

Revision ID: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from mock_erp.hashing import request_hash
from mock_erp.seed import SEED_ACCOUNTS

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

INSERT = sa.text(
    "INSERT INTO accounts (code, external_reference, name, type, usage, parent_code, "
    "currency, request_hash) VALUES (:code, :external_reference, :name, :type, :usage, "
    ":parent_code, 'RWF', :request_hash) ON CONFLICT (code) DO NOTHING"
)


def upgrade() -> None:
    bind = op.get_bind()
    for account in SEED_ACCOUNTS:
        body = {**account, "currency": "RWF"}
        bind.execute(INSERT, {**account, "request_hash": request_hash(body)})


def downgrade() -> None:
    codes = [a["code"] for a in reversed(SEED_ACCOUNTS)]
    for code in codes:
        op.execute(sa.text("DELETE FROM accounts WHERE code = :code").bindparams(code=code))
