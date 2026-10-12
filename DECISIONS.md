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

## Sync vs async

**Chosen: synchronous call to Fineract; ERP posting through a transactional outbox delivered by an in-process poller. No broker.**

- The partner gets Fineract's real result. Fineract is the system of record, so money has moved once it confirms.
- An ERP outage never blocks the partner; the ERP catches up from persisted outbox rows.
- A broker (Kafka, RabbitMQ, Celery) would still need an outbox to avoid the dual-write problem, still deliver at-least-once (so idempotency is still required), and adds infrastructure the evidence would have to cover. Not needed at this scale.
- No Redis: idempotency and locks must be transactional with business state, and a TTL would reopen the door.

## Database constraints and idempotency

- Idempotency is a Postgres row claimed with `INSERT … ON CONFLICT DO NOTHING` on `UNIQUE(operation_type, external_reference)`, storing the request hash and the final response. Never held in application memory.
- One live approval and one live disbursement per loan: partial unique index excluding `FAILED`, so a definitively failed attempt doesn't block a later one.
- Per-loan serialisation of Fineract writes with a Postgres advisory lock, because Fineract may reject or fail concurrent writes to the same loan *(verify on 1.15.0)*.
- Our reference is sent to Fineract as the transaction `externalId`, and as the `Idempotency-Key` header (Fineract 1.15.0 has `IdempotencyKeyResolver`) *(verify header name and behaviour)*.
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
