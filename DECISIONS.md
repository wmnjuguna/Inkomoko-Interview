# Decisions

This file explains the choices I made and why.

- *assumption* means I could not confirm it with the assessors, so I chose a sensible answer.
- *to check* means I read it in the Fineract source but have not yet seen it work.

## Setup

- **Fineract 1.15.0** (commit `d5636847`). It was the newest stable release when I started. It is a git submodule, so the exact commit is recorded. I never edit it.
- **JDK 21 and Gradle 8.14.5.** I took both from the Fineract tag itself, not from my own choice.
- **PostgreSQL 18.3, Alpine image.** Fineract 1.15.0 uses this version in its own Docker files, so it is tested with it. One server, with one database and one user per service.
- **Alpine images** for the build and the runtime, to keep images small.

## The Fineract plugin

### How I changed Fineract

I chose a **custom module**: a plugin that sits in Fineract's `custom/` folder. I did not edit Fineract's own code.

| Option | Good | Bad |
|---|---|---|
| **Custom module** (chosen) | Fineract supports it and ships an example. The diff is only new files. It still goes through Fineract's permission checks and audit log. | Fineract's GL table has no column for the ERP reference, so I need a separate table. |
| Edit Fineract's code | The reference sits on the GL row itself | Changes Fineract's source. Upgrades get harder. |
| Change the existing `POST /glaccounts` | Smallest change | Changes how a standard Fineract endpoint behaves |

Fineract 1.15.0 has no external reference on GL accounts. So all six Part A rules live in my plugin.

### It is a plugin, not a copy of Fineract

The plugin is built against the Fineract 1.15.0 source. At runtime it is two jar files that Fineract loads at start-up. It only uses hooks that Fineract already offers:

- Spring auto-configuration, to switch the plugin on
- Fineract's discovery of API classes and error handlers
- Fineract's command handler registry
- Fineract's hook for adding database entities from plugins
- Fineract's folder for extra database migrations

No Fineract file is changed.

### It calls Fineract's accounting code directly

The plugin calls Fineract's own Java code to create the GL account. It does not call Fineract's REST API. It runs inside the database transaction that Fineract opens for each command. So three things are saved together, or not at all: the GL account, my reference row, and Fineract's audit record. A plugin that only called Fineract's REST API would be the "thin wrapper" the brief warns about.

### One plugin only

Everything else the brief asks of Fineract uses normal settings or built-in features: `externalId` fields, lookups by external ID, loan states and transaction details. So one plugin, `glsync`, is enough. That keeps my change to Fineract small.

### Why not data tables

Fineract's data tables can't be attached to GL accounts in 1.15.0. They can only be attached to clients, groups, centers, offices, loan products, loans, savings, share products and working-capital loans. Also, data tables only store data. They can't enforce rules like "same data returns the existing account". Clients and loans don't need them either, because they already have a unique `externalId`.

### The API

`PUT` and `GET` on `/v1/erp-accounts/{erpReference}`. The body uses the same fields as Fineract's own `POST /glaccounts`, so Fineract's own checks still apply. `GET` exists so a caller can check whether a sync worked when the answer was lost, instead of sending it again.

### Parent accounts

An account can name its parent with `parentAccount`, using the parent's ERP reference. I don't accept Fineract's `parentId`, because Fineract's numeric IDs stay out of the API. The plugin checks three things:

- The parent is already synced. Parents must be synced before their children.
- The parent is a HEADER account. Fineract checks this too.
- The parent has the same type as the child. Fineract does **not** check this, so the plugin does. Without it, an income account could sit under an asset heading.

The account itself can be a HEADER or a DETAIL (sub-ledger) account. Fineract then builds its own hierarchy path.

### Requests at the same time

If something breaks a unique rule in Postgres, the whole transaction is cancelled. So the plugin can't just try, fail and try again. Instead it takes a Postgres lock on the reference and on the GL code before it reads anything. The second request waits, then sees the saved row. The locks always go in the same order, so two requests can never wait on each other forever.

The locks go through Fineract's own database connection for that transaction. This matters because Fineract opens transactions late, and a plain read could use a different connection.

Tested: 10 requests at the same time give 1 CREATED, 9 UNCHANGED, and one GL account. This ties the plugin to Postgres, which is fine because this project always uses Postgres.

### Checking `type` and `usage`

The GL table in the database only says these can't be empty. It has no rule about which values are allowed, so the database alone would accept type 9. The plugin rejects any `type` outside 1–5 and any `usage` outside 1–2 before Fineract does anything. The allowed values come from Fineract's own lists, and the error codes are Fineract's own.

I did not add a database rule (a CHECK constraint) for this. It would change a Fineract table from my plugin. The plugin's check covers every way into the API. I will only add more if something turns out to be missing.

### Permissions

The plugin's migration creates the `SYNC_GLACCOUNT` permission. The setup script will give it to the API user. That keeps all the role setup in one place.

### 409 for conflicts

Fineract normally answers rule errors with 403, and it has no 409 handler. So the plugin adds its own 409 handler.

## Sync or async

**Chosen:** the Integration Service calls Fineract directly and waits for the answer. Journal entries go to the ERP through an outbox table and a background loop. There is no message queue.

- The partner gets Fineract's real answer. Fineract is the record of truth, so once it says yes, the money has moved.
- If the ERP is down, the partner is not blocked. The ERP catches up later from the outbox.
- A message queue (Kafka, RabbitMQ, Celery) would still need an outbox to be safe. It would still deliver some messages twice, so duplicate protection would still be needed. And it is one more thing to run and prove. It is not needed at this size.
- No Redis. Duplicate checks and locks must be saved together with the business data, and a cache can expire.

### Fineract business events: looked at, not used

Fineract 1.15.0 can save an event in the same transaction as each change, then send it to ActiveMQ or Kafka.

| Option | Verdict |
|---|---|
| A. Call Fineract directly, plus our own outbox | **Chosen.** No queue to run. One place owns duplicate checks and status. Every failure can be seen and tested. Downside: a payment made in Fineract outside our service only shows up in reconciliation. |
| B. Fineract events, a queue, and a Python consumer | The next step for production, if payments reach Fineract from other places too. It catches every payment, but adds a queue, a message format and delays. |
| C. A Java listener inside my plugin | Rejected. Calling the ERP inside Fineract's transaction is unsafe, and it would move integration work into Java. |
| D. Regularly asking Fineract for new data | Rejected. Reconciliation already does this job. |

Fineract has no event for a new GL account anyway, so GL sync stays a direct call. Every payment is keyed by `externalId`. So moving from A to B later only means swapping the outbox loop for a consumer. Fineract's external events stay switched off.

## Duplicate protection *(planned, except where marked)*

- A request is saved as a database row with a unique rule on (type, `external_reference`). It stores a hash of the request and the final answer. Nothing is kept only in memory.
- Only one live approval and one live disbursement per loan. A request that clearly failed does not count, so it can be tried again.
- Writes to one loan go one at a time, using a Postgres lock per loan, because Fineract may not handle two writes to the same loan at once *(to check)*.
- Our reference goes to Fineract as the transaction's `externalId`. A key made from it goes in the `Idempotency-Key` header.

### What Fineract 1.15.0 already does

- **The `Idempotency-Key` header.** Fineract saves each key, in its own transaction, before it does the work. If the same key comes again after the work is done, it returns the first answer with `x-served-from-cache: true`. While the work is still running, it returns 425. After an error, it returns the saved error. Seen working with my plugin.
- **Unique `externalId`** on clients, loans and loan transactions.
- **What Fineract does not do, so we do it:**
  - It does not compare the request data. We compare a hash and answer 409.
  - Without the header, it makes a random key. We always send one.
  - It does not stop two different keys. We add the one-disbursement rule, the per-loan lock, and Fineract's loan state.
  - It replays errors instead of retrying. A new attempt uses a new key.
  - A crash in the middle could leave a key stuck *(to check)*. Recovery looks up the `externalId` first.

**In short:** the unique `externalId` on the transaction is the real guarantee. The header is a convenient replay on top.

### How keys are made

`Idempotency-Key = <operation>:<external_reference>:<attempt>`. It is saved before the call. The attempt number only goes up after a lookup proves nothing happened. A key is never made from the request data alone, because two real repayments can have identical data.

The ERP has its own unique rule on the reference, and its own debits-equal-credits rule. It does not trust the caller.

## Retries and partial failure *(planned)*

- We never retry a Fineract call until a lookup by `externalId` shows it did not happen.
- A request is only marked `FAILED` when we know nothing happened in Fineract: it was not reachable, it clearly refused, or a lookup found nothing.
- Sending a journal entry to the ERP is always safe to retry, because the ERP ignores duplicates.
- Wait 1 s, then 2 s, then 4 s and so on, up to 60 s, at most 8 times. Then mark it `NEEDS_RECONCILIATION`.
- No separate worker. Recovery runs at start-up, then in a loop, and on demand with `POST /admin/recover`.

## Fineract settings (Part B) *(planned)*

| Setting | Choice | Why |
|---|---|---|
| Office | `Kigali Main Office`, under the default Head Office *(assumption)* | Keeps Fineract's top office as it is |
| Currency | RWF, no decimals | RWF has no cents |
| Loan product | "SME Asset Finance": 18% a year, 12 monthly payments, declining balance, equal payments, paid out once | The values the brief suggests |
| Accounting | Cash-based | Journal entries only on payout and repayment, which matches Part H. Reconciliation is one entry per payment. It is less realistic, because no interest is booked between payments. |
| GL accounts | All owned by the ERP and synced. Codes follow the usual pattern: 1xxx assets, 2xxx liabilities, 4xxx income, 5xxx expenses. | The product needs an account of the right type for each role. I will read the product template to see which roles are required *(to check)*. |
| API user | Its own role with only the permissions the Integration Service needs | Least privilege. The admin user is only used by the setup script. |

## Journal entries (Part H) *(planned)*

- Built from how Fineract split each payment (principal, interest and so on). Never from my own interest maths.
- Payout: debit 1200 Loan Receivable, credit 1100 Bank.
- Repayment: debit 1100 Bank, credit 1200 (principal) and 4000 (interest).
- Overpayment: credit 2100 Overpayment Liability.
- Reversal: a new opposite entry with reference `<original>:REV`. Nothing is deleted.
- If a payment has fees or penalties, the entry is not guessed. The request goes to `NEEDS_RECONCILIATION`, so nothing is lost quietly.
- The ERP entry date is Fineract's transaction date *(assumption)*.

## Open questions (my assumptions)

- Does a plugin in `custom/` count as "changing or extending Fineract"? I think yes. It is Fineract's own way to extend it.
- Should a payout return 200 while the ERP entry is still pending, or 202 until it is done? I return 200 with `erp_status`.
- How do partners log in? I use one API key per partner.
- What is `FIN-2026-000001`? I assume it is a partner's `external_reference`.
- Should reconciliation include reversed payments? I assume yes, matched to their opposite entries.

## What I chose not to build

No message queue, no Redis, no separate worker, no OAuth. More will be added as the work goes on.

## Known weaknesses

To be filled in from what testing shows.
