"""Schema: accounts, journal entries and lines, fault settings, and the balance check.

Revision ID: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Runs at COMMIT, after every line of the entry is in. Python checks first and gives a clean
# 422; this trigger is the last guard for anything written around Python, such as raw SQL.
TABLES = """
CREATE TABLE accounts (
    code               text PRIMARY KEY,
    external_reference text NOT NULL UNIQUE,
    name               text NOT NULL,
    type               text NOT NULL
        CONSTRAINT accounts_type_check
        CHECK (type IN ('ASSET', 'LIABILITY', 'EQUITY', 'INCOME', 'EXPENSE')),
    usage              text NOT NULL
        CONSTRAINT accounts_usage_check CHECK (usage IN ('HEADER', 'DETAIL')),
    parent_code        text REFERENCES accounts (code),
    currency           text NOT NULL DEFAULT 'RWF',
    request_hash       text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE journal_entries (
    id                 uuid PRIMARY KEY,
    external_reference text NOT NULL UNIQUE,
    request_hash       text NOT NULL,
    entry_date         date NOT NULL,
    source             text NOT NULL
        CONSTRAINT journal_entries_source_check
        CHECK (source IN ('DISBURSEMENT', 'REPAYMENT', 'REVERSAL', 'MANUAL')),
    reverses           text REFERENCES journal_entries (external_reference),
    description        text,
    correlation_id     text,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX journal_entries_entry_date_idx ON journal_entries (entry_date);

CREATE TABLE journal_lines (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id     uuid NOT NULL REFERENCES journal_entries (id),
    account_code text NOT NULL REFERENCES accounts (code),
    debit        bigint NOT NULL DEFAULT 0,
    credit       bigint NOT NULL DEFAULT 0,
    CONSTRAINT journal_lines_one_side_check
        CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
);
CREATE INDEX journal_lines_entry_id_idx ON journal_lines (entry_id);

CREATE TABLE fault_settings (
    id            integer PRIMARY KEY CONSTRAINT fault_settings_single_row CHECK (id = 1),
    mode          text CONSTRAINT fault_settings_mode_check
        CHECK (mode IS NULL OR mode IN ('down', 'timeout', 'timeout_after_write', 'rate_limited')),
    path_prefix   text NOT NULL DEFAULT '/api',
    delay_seconds integer NOT NULL DEFAULT 10,
    remaining     integer,
    retry_after   integer NOT NULL DEFAULT 5,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
INSERT INTO fault_settings (id) VALUES (1);
"""

BALANCE_TRIGGER = """
CREATE FUNCTION check_entry_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    touched      uuid := coalesce(NEW.entry_id, OLD.entry_id);
    total_debit  bigint;
    total_credit bigint;
    header_count int;
BEGIN
    SELECT coalesce(sum(l.debit), 0), coalesce(sum(l.credit), 0),
           count(*) FILTER (WHERE a.usage = 'HEADER')
      INTO total_debit, total_credit, header_count
      FROM journal_lines l
      JOIN accounts a ON a.code = l.account_code
     WHERE l.entry_id = touched;

    IF header_count > 0 THEN
        RAISE EXCEPTION 'entry % posts to a HEADER account', touched
            USING ERRCODE = 'check_violation', CONSTRAINT = 'journal_lines_detail_only';
    END IF;
    IF total_debit <> total_credit THEN
        RAISE EXCEPTION 'entry % is not balanced: debits %, credits %',
            touched, total_debit, total_credit
            USING ERRCODE = 'check_violation', CONSTRAINT = 'journal_entries_balanced';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER journal_lines_balanced
    AFTER INSERT OR UPDATE OR DELETE ON journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_entry_balanced();
"""


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(BALANCE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS journal_lines_balanced ON journal_lines")
    op.execute("DROP FUNCTION IF EXISTS check_entry_balanced()")
    op.execute("DROP TABLE fault_settings, journal_lines, journal_entries, accounts")
