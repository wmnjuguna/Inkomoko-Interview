# Decisions

> **Status:** decisions taken at design time, with the reasoning. Items marked *assumption* were not confirmed by the assessors; items marked *verify* depend on behaviour still to be checked on Fineract 1.15.0.

## Environment

- **Fineract 1.15.0** (`d5636847`), latest stable release tag at the time of starting. Pulled as a git submodule so the exact commit is recorded in this repo; the upstream tree is never edited.
- **JDK 21, Gradle 8.14.5**, both taken from the tag itself (toolchain and wrapper), not chosen independently.
- **PostgreSQL 18.3** for every database. It's the image upstream 1.15.0 uses in its own compose files, so Fineract is run on a database version its maintainers test against. One server, one database and user per service.

## Fineract customisation mechanism

**Chosen: a custom Gradle module under Fineract's `custom/` tree, core untouched.**

| Option | For | Against |
|---|---|---|
| **Custom module** (chosen) | Fineract's documented extension path; 1.15.0 auto-includes `custom/<company>/<category>/<module>` and ships an example; diff against the tag is new files only; still runs through the command pipeline (permissions, audit) | Can't add a column to core `GLAccount`, so the reference needs a side table + join; more Spring wiring |
| Edit core (`fineract-provider` / `fineract-core`) | Reference lives on the GL row; matches "add a field to the GL entity" | Patches core sources; merge conflicts on upgrade |
| Extend existing `POST /glaccounts` | Smallest surface | Changes semantics of a core endpoint |

Checked on 1.15.0: `GLAccount` has no external-id column, so all six Part A behaviours are implemented in our module. The module is real enforcement inside Fineract's transaction boundary, not a wrapper around the existing API.

**One module (`glsync`, split into `service` and `starter`).** Every other requirement is met by configuration through Fineract's REST API, or by built-in features (`externalId`, external-ID lookups, loan states, transaction breakdowns). This keeps the diff against 1.15.0 small.

**Data tables were considered and rejected for Part A.** In 1.15.0 they can only attach to client, group, center, office, loan product, loan, savings, share product and working-capital loan, not GL accounts. They also store data without enforcing rules, and Part A needs logic (compare the payload, return the existing account or 409, check a permission). Identities don't need them either, because clients and loans already have a unique `externalId`.

**It is a plugin, not a fork.** `glsync` is a Fineract custom module compiled against the pinned 1.15.0 source and loaded at runtime from `/app/libs` via `loader.path`. It uses only Fineract's own extension points: Spring auto-configuration, `@Path`/`@Provider` bean discovery, the `@CommandType` handler registry, `EntityManagerFactoryCustomizer` for its entity, and `db/custom-changelog` for its Liquibase changes. No core file is edited.

**It calls core accounting in-process.** The sync service calls core's `GLAccountWritePlatformService.createGLAccount` and `GLAccountRepository` directly, inside the command transaction Fineract opens in `CommandSourceService.processCommandAndSaveResult`. The core GL row, our reference row and Fineract's audit row commit together. Calling Fineract's REST API from Java instead would be the "thin wrapper" the brief warns against.

**API: `PUT`/`GET /v1/erp-accounts/{erpReference}`.** It names the thing being mirrored. The body is exactly core's GL body, so core's validator applies unchanged. The module adds one rule: `usage` is required, because core's validator allows null but the column is NOT NULL. GET exists so a lost sync response can be resolved by lookup.

**Concurrency: advisory locks, not "retry after a unique violation".** Postgres aborts the whole command transaction on a constraint violation, so the handler can't recover from one. The service takes `pg_advisory_xact_lock` on the reference and GL code (sorted) through Fineract's `JdbcTemplate`, which is bound to the transaction's own connection. Fineract's dialect starts database transactions lazily, so a plain read could otherwise land on another connection. Verified: 10 parallel identical calls give 1 CREATED, 9 UNCHANGED and one GL account. Advisory locks tie the module to Postgres, which this deployment pins.

**The migration creates `SYNC_GLACCOUNT`; the setup script grants it.** Role setup stays in one place (Part B).

**409 for conflicts.** Core maps rule violations to 403 (`PlatformDomainRuleExceptionMapper`) and has no 409 mapper, so the module registers its own.

## Sync vs async

**Chosen: synchronous call to Fineract; ERP posting through a transactional outbox delivered by an in-process poller. No broker.**

- The partner gets Fineract's real result. Fineract is the system of record, so money has moved once it confirms.
- An ERP outage never blocks the partner; the ERP catches up from persisted outbox rows.
- A broker (Kafka, RabbitMQ, Celery) would still need an outbox to avoid the dual-write problem, still deliver at-least-once (so idempotency is still required), and adds infrastructure the evidence would have to cover. Not needed at this scale.
- No Redis: idempotency and locks must be transactional with business state, and a TTL would reopen the door.

### Fineract business events: considered, not used

1.15.0 has in-process business event listeners, plus optional external events. External events are written to `m_external_event` in the same transaction as the change, then published as Avro to ActiveMQ or Kafka by the "Send Asynchronous Events" job, each with its own `idempotencyKey`.

| Option | Verdict |
|---|---|
| A. Command-driven + our outbox | **Chosen.** No broker; one owner for idempotency and state; every failure scenario is a visible, testable state. Downside: transactions made outside the integration reach the ERP only through reconciliation (MISSING_IN_ERP). |
| B. Fineract external events → broker → Python consumer (inbox de-duplication on the event `idempotencyKey`, ERP unique reference as the final guard) | The production next step if transactions reach Fineract from other channels. It brings Fineract's own outbox and catches every transaction, but adds a broker, Avro, at-least-once delivery and scheduled-job delay. |
| C. Java listener in our module | Rejected. A remote call inside Fineract's transaction is unsafe, and it would move integration logic into Java and bloat the change. |
| D. Poll Fineract | Rejected. It duplicates reconciliation. |

There is no business event for GL account creation, so GL sync stays a direct call. Because every transaction is keyed by `externalId`, moving from A to B later only replaces the outbox poller with a consumer. External events stay switched off in our image.

## Database constraints and idempotency

- Idempotency is a Postgres row claimed with `INSERT … ON CONFLICT DO NOTHING` on `UNIQUE(operation_type, external_reference)`, storing the request hash and the final response. Never held in application memory.
- One live approval and one live disbursement per loan: partial unique index excluding `FAILED`, so a definitively failed attempt doesn't block a later one.
- Per-loan serialisation of Fineract writes with a Postgres advisory lock, because Fineract may reject or fail concurrent writes to the same loan *(verify on 1.15.0)*.
- Our reference is sent to Fineract as the transaction `externalId`, and a key derived from it as the `Idempotency-Key` header.

### What Fineract 1.15.0 provides natively (read in source; to be confirmed in M8)

- **`Idempotency-Key` header** on write commands. The key is claimed in `m_portfolio_command_source`, unique on (`action_name`, `entity_name`, `idempotency_key`), with status `UNDER_PROCESSING`, in its own transaction. The command's effect and its stored result then commit together (`PROCESSED`), or the error is stored (`ERROR`). A repeat returns the original status and body with `x-served-from-cache: true`; a repeat while running returns 425; a repeat after an error returns the stored error. The key column is VARCHAR(50).
- **Unique `external_id`** on `m_client`, `m_loan` and `m_loan_transaction`.
- **Gaps we cover ourselves:**
  - No payload comparison, so we check `request_hash` and return 409 before calling Fineract.
  - With no header, Fineract generates a random key, so we always send one.
  - Different keys aren't stopped, so we add a one-disbursement-per-loan index, a per-loan lock and Fineract's loan state.
  - Errors are replayed, not retried, so a new attempt uses a new key.
  - A crash mid-command may leave a key stuck `UNDER_PROCESSING`, so recovery looks up by `externalId` first.

**Consequence:** the unique transaction `externalId` is the real guarantee, and the header is a replay cache on top.

### Key derivation

Keys are deterministic and stored before the call: `Idempotency-Key = <OP>:<external_reference>:<attempt>`. The attempt number increases only after a lookup by `externalId` proves nothing happened. Keys are never derived from the payload alone, because two genuine identical repayments must both post.
- The ERP enforces its own `UNIQUE(external_reference)` and balance rule; it doesn't trust the caller.

## Retries and partial failure

- A Fineract call is never retried until a lookup by `externalId` says the operation didn't happen (`GET /loans/external-id/{loan}/transactions/external-id/{tx}`, present in 1.15.0).
- `FAILED` is used only when we know nothing happened in Fineract: connection refused before sending, a definitive business 4xx, or a lookup that returns not found.
- ERP posting is always safe to retry because the ERP is idempotent on the reference.
- Backoff 1 s → 60 s with jitter, max 8 attempts, then `NEEDS_RECONCILIATION`.
- Recovery without a worker: startup sweep + in-process loop + `POST /admin/recover`.

## Fineract configuration (Part B)

| Item | Decision | Why |
|---|---|---|
| Office | `Kigali Main Office`, created under the default Head Office *(assumption)* | Keeps Fineract's root office intact |
| Currency | RWF, 0 decimal places, `inMultiplesOf` 1 | RWF has no minor unit |
| Loan product | "SME Asset Finance": 18% p.a., 12 monthly repayments, declining balance, equal instalments, single disbursement | Values suggested in the brief |
| Accounting | Cash-based | Journal entries only on disbursement and repayment, matching the Part H mapping one-to-one; reconciliation is one-to-one per transaction. Not IFRS-realistic: no interest accrual between payments |
| GL accounts | 1100 Bank, 1200 Loan Receivable, 1300 Transfers in Suspense, 2100 Overpayment Liability, 4000 Interest Income, 4100 Fee Income, 4200 Penalty Income, 4300 Recovery Income, 5100 Loan Write-off Expense, all owned by the ERP and synced | A cash-based product needs every role mapped, and every mapped account must be a synced one *(verify the required roles against the 1.15.0 product template)* |
| API user | Dedicated role with only the permissions the Integration Service needs | Least privilege; the admin user is used only by bootstrap to create it |

## Accounting mapping (Part H)

- Entries are derived from Fineract's transaction breakdown (`principalPortion`, `interestPortion`, …), never from our own interest calculation.
- Disbursement: DR 1200 Loan Receivable / CR 1100 Bank.
- Repayment: DR 1100 Bank / CR 1200 (principal) / CR 4000 (interest).
- Overpayment: `overpaymentPortion` → CR 2100 Overpayment Liability.
- Reversal: a contra entry with reference `<original>:REV`; nothing is deleted.
- A non-zero fee or penalty portion is refused by the mapping and sent to `NEEDS_RECONCILIATION`, so nothing is dropped silently.
- The ERP `entry_date` is the Fineract transaction date *(assumption)*.

## Open questions (recorded as assumptions)

- Does a `custom/` module count as "modify or extend Fineract"? Our view: yes, it is the documented extension path.
- Should disbursement return 200 while the ERP is pending, or 202 until completed? Assumed 200 with `erp_status`.
- Partner auth: assumed a static API key per partner.
- What does `FIN-2026-000001` identify? Assumed to be a partner `external_reference`.
- Should reconciliation include reversed transactions? Assumed yes, matched against contra entries.

## What we chose not to build

To be completed. Currently: no message broker, no Redis, no separate worker container, no OAuth.

## Known weaknesses

To be completed from what testing shows.
