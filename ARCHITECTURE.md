# Architecture

How the system fits together, and why. [README.md](README.md) runs it, [API.md](API.md) lists the endpoints, [DECISIONS.md](DECISIONS.md) weighs the trade-offs and [SIMULATIONS.md](SIMULATIONS.md) breaks it on purpose.

## The parts

```mermaid
flowchart LR
  subgraph compose [docker compose, every port on localhost]
    IS["Integration Service :8091"]
    FP["Fault proxy :8081 data, :8092 admin"]
    subgraph FIN ["Fineract 1.15.0 :8080"]
      PL[glsync plugin]
    end
    ERP["Mock ERP :8090"]
    DB[("Postgres 18.3 :5432")]
  end
  C[Client system] -->|REST, business ids only| IS
  IS -->|every Fineract call| FP
  FP --> FIN
  IS -->|journal entries, via the outbox| ERP
  ERP -.->|chart of accounts| IS
  IS --- DB
  FIN --- DB
  ERP --- DB
```

The client system talks only to the Integration Service. Fineract keeps clients, loans and balances. The Mock ERP owns the chart of accounts and stores the journal entries. One Postgres server holds four databases, each with its own user: `fineract_tenants`, `fineract_default`, `integration_db` and `erp_db`.

There is no queue and no separate worker. An outbox table and a background thread do that job. An `X-Correlation-ID` follows every request through every log line, even the ERP send seconds later.

## How data moves

```mermaid
flowchart TB
  subgraph A [GL accounts, one way]
    E1[Mock ERP chart] --> I1[Integration Service] --> G1[glsync plugin] --> F1[Fineract GL accounts]
  end
  subgraph B [Client requests]
    P2[Client system] --> I2[Integration Service] --> F2[Fineract]
    I2 --> O2[(outbox)] --> E2[Mock ERP journal]
  end
  subgraph C [Reconciliation, read only]
    I3[Integration Service] -.->|reads| F3[Fineract transactions]
    I3 -.->|reads| E3[ERP journal entries]
    I3 -.->|reads| T3[(own tables)]
  end
```

Three flows, and they never mix. The chart only flows from the ERP into Fineract, and only creates accounts. A money movement reaches the ERP from the outbox, never straight from the request. Reconciliation reads everything and changes nothing.

## GL sync

```mermaid
sequenceDiagram
  participant S as Integration Service
  participant DB as integration_db
  participant PL as glsync plugin
  participant FDB as fineract_default
  S->>DB: take the run lock, pg_try_advisory_lock
  Note over S,DB: already taken: 202 with the current state
  S->>DB: gl_sync row for GL-1200, SYNC_PENDING, body hash
  opt sent before without a clear answer
    S->>PL: GET /v1/erp-integration/accounts/GL-1200
    PL-->>S: 200 mapped, so SYNCED. 404, so send it
  end
  S->>PL: PUT /v1/erp-integration/accounts/GL-1200, Idempotency-Key GLS:GL-1200:1:ab12cd34
  PL->>FDB: pg_advisory_xact_lock on the reference and the GL code
  PL->>FDB: glsync_external_ref by reference
  alt new reference
    PL->>FDB: create the GL account and the reference row, one transaction
    PL-->>S: 200 CREATED
  else same reference, same data
    PL-->>S: 200 UNCHANGED
  else other data, code taken, or parent not synced
    PL-->>S: 409
  end
  S->>DB: SYNCED, or FAILED with the reason
```

Fineract takes plugins and custom modules, so the customisation is a plugin beside Fineract's code, never a change to its core. It lives under `custom/inkomoko/glsync` and only the new `SYNC_GLACCOUNT` permission may call it. Fineract's GL table has no column for an outside reference, so the plugin keeps its own in `glsync_external_ref`. The advisory locks make two syncs of one account queue up instead of racing.

Sync walks the chart parents first. An unclear earlier attempt is looked up, never sent again blindly. Sync only creates: an account the ERP changes later stays `SYNCED`, with a note in `last_error`.

## What we store

```mermaid
erDiagram
  operations ||--o{ operation_events : history
  operations ||--o| outbox : "journal entry"
  clients ||--o{ loans : owns
  gl_sync ||--o| glsync_external_ref : "same reference, Fineract side"
  operations {
    bigint id PK
    text op_key UK
    text op_type
    text external_reference
    text status
    text loan_reference
    int attempts
    jsonb response_body
  }
  operation_events {
    text from_status
    text to_status
    text detail
    timestamptz at
  }
  clients {
    text client_uid PK
    text display_name
    bigint fineract_client_id
  }
  loans {
    text loan_reference PK
    text client_uid FK
    bigint principal
    bigint fineract_loan_id
  }
  outbox {
    jsonb entry
    text status
    int attempts
    timestamptz next_attempt_at
  }
  gl_sync {
    text erp_reference PK
    text erp_code UK
    text status
    text payload_hash
    bigint fineract_gl_id
  }
  glsync_external_ref {
    bigint gl_account_id UK
    varchar external_reference UK
    char payload_hash
  }
```

`operations` is the heart of it: one row per client request, keyed by `op_key`, like `DSB:FIN-2026-000123`. The same key is Fineract's `externalId` and the ERP's `external_reference`, so reconciliation needs no lookup table. Each row keeps the request hash, the status and the exact answer we gave. Fineract's numeric ids stay here and never reach a response.

Unique rules do most of the duplicate protection. `(op_type, external_reference)` is unique. Partial indexes allow one live client creation per `client_uid`, one approval per loan and one disbursement per loan.

## The life of a request

```mermaid
stateDiagram-v2
  [*] --> RECEIVED: request saved
  RECEIVED --> FINERACT_PENDING: claimed
  FINERACT_PENDING --> FINERACT_DONE: Fineract said yes, or a lookup found it
  FINERACT_PENDING --> FAILED: refused, not sent, or a lookup found nothing
  FINERACT_PENDING --> NEEDS_RECONCILIATION: 8 failed lookups
  FAILED --> FINERACT_PENDING: same reference sent again
  FINERACT_DONE --> ERP_PENDING: journal entry in the outbox
  FINERACT_DONE --> COMPLETED: nothing for the ERP
  ERP_PENDING --> COMPLETED: the ERP saved it, or already had it
  ERP_PENDING --> NEEDS_RECONCILIATION: 8 failed sends, ERP 409 or 422, or no entry built
  NEEDS_RECONCILIATION --> COMPLETED: repaired by hand
  note right of FINERACT_PENDING: 504 if the answer was unclear, then 202 on a repeat
  note right of FAILED: 503 nothing sent, 422 or 409 refused
  note right of COMPLETED: 200 or 201, a repeat gets the saved answer
  note right of NEEDS_RECONCILIATION: a repeat gets 409
```

Every status change goes through one table of allowed moves in `state_machine.py`. Anything else raises, and a unit test tries every pair. `begin()` inserts with `ON CONFLICT DO NOTHING` and, if the row exists, decides from it. Different data or a different API key gets `409`. A `FAILED` row is reused with the attempt number raised, so ten retries at once make one new attempt.

## A disbursement, step by step

```mermaid
sequenceDiagram
  participant C as Client system
  participant S as Integration Service
  participant DB as integration_db
  participant F as Fineract
  participant L as Background loop
  participant E as Mock ERP
  C->>S: POST /api/loans/LN-2026-000001/disburse, external_reference FIN-1
  S->>DB: begin: insert DSB:FIN-1, RECEIVED to FINERACT_PENDING
  Note over S,DB: known reference: saved answer, 202 or 409. Another live DSB: 409
  S->>DB: take the loan's advisory lock
  opt attempt 2 or later
    S->>F: GET transaction by externalId DSB:FIN-1
    Note over S,F: found: it is ours, finish without a new write
  end
  S->>F: GET disbursement template, amount is the approved principal
  S->>F: POST disburse, externalId DSB:FIN-1, Idempotency-Key DSB:FIN-1:1
  alt 200
    F-->>S: transaction created
  else timeout, 5xx or 425
    S->>F: GET transaction by externalId
    Note over S,F: not there yet: stays FINERACT_PENDING, caller gets 504
  else not reachable, or refused
    Note over S,DB: FAILED. 503 nothing sent, 409 or 422 refused
  end
  S->>F: GET the transaction back, and the loan
  S->>DB: one transaction: FINERACT_DONE, saved answer, outbox row, ERP_PENDING
  S->>DB: release the lock
  S-->>C: 200, status ERP_PENDING
  L->>DB: claim due outbox rows, SKIP LOCKED, 120 s lease
  L->>E: POST /api/journal-entries, external_reference DSB:FIN-1
  E-->>L: 201 created, or 200 already there
  L->>DB: outbox SENT, operation COMPLETED
  loop every 5 s, the recovery pass
    L->>DB: claim FINERACT_PENDING rows older than 180 s
    L->>F: GET transaction by externalId, never send again
    L->>DB: found: finish as above. Not found: FAILED
  end
```

The loan's advisory lock is held until the result commits. The template is read under it because it sets the amount and must not go stale. A success is read back, so the answer and the journal entry come from Fineract's own transaction. A crash before the final commit leaves `FINERACT_PENDING`, and a crash after it leaves an outbox row. The background loop finishes both.

The loop runs in each gunicorn worker, every 5 seconds and once at start-up. An ERP outage backs off 1, 2, 4, up to 60 seconds with jitter, 8 times. A `429` waits for `Retry-After` without counting. A repayment takes the same path, and Fineract must also show the loan `ACTIVE`.

## Paying out once

Five layers stop a loan being paid out twice. Three are mine, two are Fineract's, and [SIMULATIONS.md](SIMULATIONS.md) hammers the first three.

1. The saved answer per reference: a repeat never reaches Fineract.
2. The one-live-disbursement index: a second reference gets `409 LOAN_ALREADY_DISBURSED`, even mid-flight.
3. The per-loan advisory lock: two writes to one loan never run at once.
4. Fineract's unique `externalId` and our per-attempt `Idempotency-Key`: a retry cannot create a second transaction.
5. Fineract's own loan state: a single-tranche loan pays out once, whatever path the request took.

Only the last layer covers writes that bypass us.

## Where journal entries come from

`mapping/journal.py` turns Fineract's transaction into an ERP entry, and nothing more. A disbursement debits Loan Receivable `1200` and credits Bank `1100`. A repayment debits Bank and credits Loan Receivable, Interest Income `4100` and Overpayment Liability `2100` by the portions Fineract reports. The codes match the loan product's GL roles, so I never do my own interest maths. Anything unexpected, like fees or a reversal, builds no entry and goes to `NEEDS_RECONCILIATION` instead of a guess.

## The Mock ERP

A small Flask app on the same stack as the Integration Service. It seeds a chart of four HEADER groups and nine DETAIL accounts. Entries must balance, checked in Python for a clear `422` and again by a Postgres trigger at commit. A racing repeat of the same `external_reference` answers `200`. One row in `fault_settings` switches it to `down`, `timeout`, `timeout_after_write` or `rate_limited`.

## Breaking things on purpose

The fault proxy sits in front of Fineract, with data on port 8081 and admin on 8092. `down` refuses connections and `delay` slows everything. `timeout_after_send` lets the write reach Fineract, drops the answer and then goes down, so the follow-up lookup fails too.

The crash hook in `faults.py` covers the one gap the proxy cannot reach: a crash after the Fineract result commits and before the ERP call. It is off unless `FAULT_CRASH_AFTER_FINERACT_DONE` holds a matching reference prefix. It kills the gunicorn master, compose restarts the container, and the start-up pass sends the entry left behind.

## Reconciliation

```mermaid
flowchart LR
  R["POST /api/reconciliation"] --> F["Fineract: transactions of our loans"]
  R --> E["Mock ERP: journal entries"]
  R --> T[("operations and outbox")]
  F --> K{"match on the op key"}
  E --> K
  T --> K
  K --> M[MATCHED]
  K --> A[AMOUNT_MISMATCH]
  K --> ME[MISSING_IN_ERP]
  K --> MF[MISSING_IN_FINERACT]
  K --> I[IN_FLIGHT]
```

Reconciliation takes up to 31 days or up to 100 references. `MATCHED` means the ERP entry equals what my mapping builds from Fineract, line for line. `IN_FLIGHT` means the outbox will still send it, so it is not reported as missing. If either side cannot be read the call fails with `502`, `503` or `504`, because a half-read report would show false gaps.
