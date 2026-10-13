# Decisions

This file explains the choices I made and why.

- *assumption* means I could not confirm it with the assessors, so I chose a sensible answer.
- *to check* means I read it in the Fineract source but have not yet seen it work.

## Setup

- **Fineract 1.15.0** (commit `d5636847`). It was the newest stable release when I started. It is a git submodule, so the exact commit is recorded. I never edit it.
- **JDK 21 and Gradle 8.14.5.** I took both from the Fineract tag itself, not from my own choice.
- **PostgreSQL 18.3, Alpine image.** Fineract 1.15.0 uses this version in its own Docker files, so it is tested with it. One server, with one database and one user per service.
- **Alpine images** for the build and the runtime, to keep images small.
- **Python 3.13** for the Mock ERP, on `python:3.13.16-alpine3.24`. Packages are locked with uv (`uv.lock`). The code is checked with ruff and with mypy in strict mode.

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

`PUT` and `GET` on `/v1/erp-integration/accounts/{erpReference}`. The body uses the same fields as Fineract's own `POST /glaccounts`, so Fineract's own checks still apply. `GET` exists so a caller can check whether a sync worked when the answer was lost, instead of sending it again.

### A separate path for the integration layer

All integration endpoints live under their own path: `/fineract-provider/api/v1/erp-integration/...`. They do not mix with Fineract's core APIs, such as `/api/v1/glaccounts` or `/api/v1/loans`.

Why:

- **It keeps the integration layer apart from the core APIs.** Anyone can see at a glance which endpoints are mine and which are Fineract's.
- **Access can be limited by path.** A gateway or proxy in front of Fineract can let the integration traffic reach only `/api/v1/erp-integration/**`, and keep the core APIs closed to it.
- **It has room to grow.** Later integration endpoints go under the same prefix.
- **It can change on its own.** The prefix can be versioned separately from Fineract's own APIs.

The path still sits under `/fineract-provider/api`. That is on purpose. Only paths under `/api` go through Fineract's login, tenant check, permission check and audit log. A path outside `/api` would skip all of that, and I would have to rebuild it by hand.

Who can reach Fineract at all is a separate question. It is controlled by the network, not by paths. For now Fineract's port is only open on `localhost`, so I can use Swagger UI and run the tests. Before I submit, I will close it, so that only the Integration Service can call Fineract.

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

## The Mock ERP

### Flask, not FastAPI

My first plan said FastAPI for both Python services. I chose Flask instead, first for the Mock ERP and then for the Integration Service too (see below).

| Option | Good | Bad |
|---|---|---|
| **Flask 3 + flask-openapi3 + Pydantic** (chosen) | Jinja2 comes with Flask, so the web UI needs nothing extra. flask-openapi3 checks each request against a Pydantic model, answers `422` when it does not match, and builds the OpenAPI spec from the same models. | One more library (flask-openapi3) to get what FastAPI has built in |
| FastAPI | Typed checks and the spec come built in. Async. | Server-rendered pages need Jinja2 added and set up. Async brings nothing here. |

Why Flask is enough here:

- **The usual gap is closed.** Plain Flask has no typed input checks and no generated spec. flask-openapi3 and Pydantic add both. Bad input gets `422` in the same error format as every other error.
- **The service is small and synchronous.** Each call is one short database transaction. Nothing waits on another service.
- **The slow faults are on purpose.** The `timeout` modes hold a request open for up to 120 seconds. Gunicorn runs 2 workers with 4 threads each, so a held request blocks one thread, not the whole ERP.

### Debits equal credits, checked in Python and in the database

Python checks the balance first. That gives a clear `422` with the totals. A Postgres trigger checks it again when the transaction commits. It also refuses lines on HEADER accounts. The trigger is "deferred", which means it runs at commit, after all the lines of the entry are in. So a write that skips the Python code, such as raw SQL, still can't save an unbalanced entry. The ERP does not trust its caller, and this is the last guard.

### One row for the fault switch

The fault setting is one row in the database, not a value in memory. Gunicorn runs 2 separate processes. A value in memory would only change in one of them, so half the calls would ignore the fault. With one row, every process sees the same setting.

A fault can be limited to a number of calls. Each call takes one from the count in a single `UPDATE … RETURNING old.*` statement. The row lock makes calls at the same time wait for each other, so the count is exact. `RETURNING old.*` is new in Postgres 18. That is fine, because this project always uses Postgres 18.

### The API key is checked before the fault switch

A call without a valid key gets `401` and stops. It never reaches the fault switch. So a stray call without a key can't use up a fault that a test set for "the next 2 calls".

### A light CSRF guard on the web UI

The UI under `/ui` has no login. Every `POST` there must carry the `HX-Request` header, which HTMX sends. Without it the answer is `400`. A plain form on another website can't set that header, so it can't change faults or add accounts through someone's browser.

This is only safe because the ERP is only open on `localhost`. If the UI were ever reachable from other machines, it would need a real login and real CSRF tokens.

## The Integration Service

### Flask, the same as the Mock ERP

The Integration Service uses the same stack as the Mock ERP: Flask, flask-openapi3, Pydantic, SQLAlchemy Core, Alembic and Gunicorn. The reasons above apply here too. There is one more: one stack means one way to do validation, errors, logs, migrations and tests. The patterns are already built and tested in the Mock ERP, so the new service reuses them instead of learning a second framework.

### The ERP reference names the operation

Every money movement gets one reference, shaped `<operation>:<partner reference>`. For example `DSB:FIN-2026-000001` for a disbursement, or `RPY:FIN-2026-000007` for a repayment. A reversal adds `:REV`.

The same string is used in three places:

- the Integration Service's own record of the operation,
- the Fineract loan transaction's `externalId`,
- the ERP journal entry's `external_reference`.

So reconciliation matches the systems on one value, with no lookup table. The prefix also keeps references unique per operation type, which Part F asks for. A partner may reuse `FIN-2026-000001` for a loan application and later for its disbursement, and they stay two different operations.

I chose this over storing the partner's reference as it is. That would need a separate rule whenever one partner reference leads to more than one entry, such as a payment and its reversal.

The operation codes are `CLI` (new client), `APP` (loan application), `APR` (approval), `DSB` (disbursement) and `RPY` (repayment). Only `DSB` and `RPY` (and their reversals) reach the ERP.

### A guard inside Fineract, as a stretch item

Fineract's loan state already refuses a second disbursement on a single-disbursement loan. That is the last line of defence today. I plan a small extra guard inside Fineract, so even a broken caller can't pay a loan out twice. It is a time-boxed stretch item, built only after the required tiers pass. If it doesn't fit in the time, I will write it up here as a known gap, with the evidence.

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

`Idempotency-Key = <operation>:<external_reference>:<attempt>`, for example `DSB:FIN-2026-000001:1`. The transaction's `externalId` is the same value without the attempt number. The key is saved before the call. The attempt number only goes up after a lookup proves nothing happened. A key is never made from the request data alone, because two real repayments can have identical data.

The ERP has its own unique rule on the reference, and its own debits-equal-credits rule. It does not trust the caller. (Built and tested.)

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
