"""Schema: operations (one row per partner request), their history, clients, loans, and the
outbox of journal entries for the ERP.

Revision ID: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Columns named fineract_* hold Fineract's numeric ids. They are for our own use only and
# never appear in a public response.
TABLES = """
CREATE SEQUENCE loan_reference_seq;

CREATE TABLE operations (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    op_type                 text NOT NULL
        CONSTRAINT operations_op_type_check CHECK (op_type IN ('CLI', 'APP', 'APR', 'DSB', 'RPY')),
    external_reference      text NOT NULL,
    op_key                  text NOT NULL CONSTRAINT operations_op_key_key UNIQUE,
    partner                 text NOT NULL,
    request_hash            text NOT NULL,
    status                  text NOT NULL
        CONSTRAINT operations_status_check CHECK (status IN ('RECEIVED', 'FINERACT_PENDING',
            'FINERACT_DONE', 'ERP_PENDING', 'COMPLETED', 'FAILED', 'NEEDS_RECONCILIATION')),
    client_uid              text,
    loan_reference          text,
    attempts                integer NOT NULL DEFAULT 0,
    next_attempt_at         timestamptz,
    last_error              text,
    response_status         integer,
    response_body           jsonb,
    fineract_resource_id    bigint,
    fineract_transaction_id bigint,
    erp_status              text
        CONSTRAINT operations_erp_status_check
        CHECK (erp_status IN ('PENDING', 'SENT', 'NEEDS_RECONCILIATION')),
    erp_transaction_id      text,
    correlation_id          text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT operations_type_reference_key UNIQUE (op_type, external_reference),
    CONSTRAINT operations_op_key_check CHECK (op_key = op_type || ':' || external_reference),
    CONSTRAINT operations_client_uid_check CHECK (op_type <> 'CLI' OR client_uid IS NOT NULL),
    CONSTRAINT operations_loan_reference_check
        CHECK (op_type NOT IN ('APR', 'DSB', 'RPY') OR loan_reference IS NOT NULL)
);

-- Only one live (not FAILED) client creation per client_uid, approval per loan and
-- disbursement per loan. A request that clearly failed does not count.
CREATE UNIQUE INDEX operations_one_live_client ON operations (client_uid)
    WHERE op_type = 'CLI' AND status <> 'FAILED';
CREATE UNIQUE INDEX operations_one_live_approval ON operations (loan_reference)
    WHERE op_type = 'APR' AND status <> 'FAILED';
CREATE UNIQUE INDEX operations_one_live_disbursement ON operations (loan_reference)
    WHERE op_type = 'DSB' AND status <> 'FAILED';
CREATE INDEX operations_external_reference_idx ON operations (external_reference);
CREATE INDEX operations_status_idx ON operations (status, next_attempt_at);

CREATE TABLE operation_events (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operation_id   bigint NOT NULL REFERENCES operations (id),
    from_status    text,
    to_status      text NOT NULL,
    detail         text,
    correlation_id text,
    at             timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX operation_events_operation_idx ON operation_events (operation_id, id);

CREATE TABLE clients (
    client_uid         text PRIMARY KEY,
    external_reference text NOT NULL,
    display_name       text NOT NULL,
    first_name         text,
    last_name          text,
    fineract_client_id bigint UNIQUE,
    status             text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE loans (
    loan_reference     text PRIMARY KEY,
    client_uid         text NOT NULL REFERENCES clients (client_uid),
    external_reference text NOT NULL,
    fineract_loan_id   bigint UNIQUE,
    principal          bigint NOT NULL CONSTRAINT loans_principal_check CHECK (principal > 0),
    status             text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbox (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operation_id       bigint NOT NULL UNIQUE REFERENCES operations (id),
    entry              jsonb NOT NULL,
    status             text NOT NULL DEFAULT 'PENDING'
        CONSTRAINT outbox_status_check
        CHECK (status IN ('PENDING', 'SENT', 'NEEDS_RECONCILIATION')),
    attempts           integer NOT NULL DEFAULT 0,
    next_attempt_at    timestamptz NOT NULL DEFAULT now(),
    last_error         text,
    erp_transaction_id text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_status_next_attempt_idx ON outbox (status, next_attempt_at);
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute(
        "DROP TABLE outbox; DROP TABLE loans; DROP TABLE clients; "
        "DROP TABLE operation_events; DROP TABLE operations; DROP SEQUENCE loan_reference_seq"
    )
