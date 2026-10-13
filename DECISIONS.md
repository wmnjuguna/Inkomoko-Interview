# Decisions

This file explains the choices I made and why.

- *assumption* means I could not confirm it with the assessors, so I chose a sensible answer.
- *to check* means I read it in the Fineract source but have not yet seen it work.

## Setup

- **Fineract 1.15.0** (commit `d5636847`). It was the newest stable release when I started. It is a git submodule, so the exact commit is recorded. I never edit it.
- **JDK 21 and Gradle 8.14.5.** I took both from the Fineract tag itself, not from my own choice.
- **PostgreSQL 18.3, Alpine image.** Fineract 1.15.0 uses this version in its own Docker files, so it is tested with it. One server, with one database and one user per service.
- **Alpine images** for the build and the runtime, to keep images small.
- **Python 3.13** for the Mock ERP and the Integration Service, on `python:3.13.16-alpine3.24`. Packages are locked with uv (`uv.lock`). The code is checked with ruff and with mypy in strict mode.

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

The plugin's migration creates the `SYNC_GLACCOUNT` permission. The setup step gives it to the API user's role. That keeps all the role setup in one place.

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

### GL sync is started by a call

**Chosen:** GL sync runs when someone calls `POST /api/accounts/sync` (or the one-account route). The setup step calls the same code once, before it creates the loan product.

| Option | Verdict |
|---|---|
| **An explicit call** (chosen) | Each run does the same steps in the same order, so the result is easy to check. Setup can sync the chart and then check every account before the loan product. |
| A timer | Sync only creates accounts. Changes and closures are out of scope, so there is nothing to keep checking for. |
| An event from the ERP | The Mock ERP has no events, and it would add a queue. |

Rules of one run:

- **Parents first.** A child is skipped while its parent is not synced.
- **One run at a time.** A Postgres advisory lock covers the whole run. A second caller gets `202` with the current state and sends nothing. Tested with 4 calls at the same time: each account was sent once.
- **Never resend blindly.** If a send got no clear answer (timeout, `5xx`), the account is looked up on the plugin first. It is only sent again if the lookup shows it is not there.
- **Fineract down.** The run stops calling Fineract and leaves the rest `SYNC_PENDING`. Calling the sync again finishes the job. There is no background retry for GL sync.
- **Keys.** `Idempotency-Key: GLS:<reference>:<attempt>:<hash8>`. `hash8` is the start of the hash of the body. The attempt number changes on each new attempt, so an old cached failure is never replayed. The hash part means different data never gets the cached answer for old data.
- **The ERP changes an account.** A synced account stays `SYNCED`, with the warning `changed in ERP; updates are out of scope`. Fineract is not called. Its children and the loan product can still use it. An account Fineract never accepted takes the new data and is sent.
- **What partners see.** `last_error` holds only our own words and Fineract error codes. It never holds Fineract's own text or body, which can include Fineract ids.

### The partner API (Parts C to E)

- **Our ids only.** Partners use `client_uid`, `loan_reference` and `external_reference`. Fineract's numeric ids are kept in our tables for our own use and appear in logs, never in a response. `evidence/P2/id-leak.txt` shows the check on the OpenAPI spec and on every answer of the runs. `evidence/P2.5/id-leak.txt` repeats it with the form endpoints, whose Fineract templates are full of ids (office, product, payment types): every option value in a form is a name or a code.
- **`loan_reference` is ours.** We issue `LN-<year>-<6 digits>` from a Postgres sequence, with the year from the database's date. It is the loan's `externalId` in Fineract. If Fineract already holds that reference (for example after `integration_db` was rebuilt but Fineract was not), a new one is issued, up to 3 times.
- **The office** is found by name through `GET /clients/template`. The API user has no permission to read `/offices`, and I did not want to add one only for this.
- **Approval and payout amounts come from Fineract.** The approval is for the amount in Fineract's approval template (the applied principal). There is no partial approval. The payout is for the amount in Fineract's disbursement template (the approved amount). The partner sends neither. See "Fineract templates are the source of the rules" below.
- **Dates** use the Kigali date, the same "today" Fineract uses for this tenant. A future transaction date, or a past expected disbursement date, gets `422` before anything is saved.
- **A replay answers `200`**, even for a create whose first answer was `201`. That is the brief's Part F rule. The saved body is returned unchanged, so `status` and `erp_status` in it are the values from the first answer. `GET /api/transactions/{external_reference}` shows the latest.
- **The first unclear answer is `504`, repeats are `202`.** The request itself gets `504 FINERACT_TIMEOUT`, because we are still finding out. Every repeat gets `202` until recovery decides.
- **`GET /api/loans/{loan_reference}` reads Fineract live.** The balances and `payoff_amount` are Fineract's own figures, not ours. In D-1, paying exactly `payoff_amount` closed the loan to `Closed (obligations met)` the same day.

### Fineract templates are the source of the rules

**Chosen:** before each write, the service reads Fineract's own template for that step and takes the rules and amounts from it. Three read-only form endpoints show the same templates to partners (`GET /api/clients/template`, `GET /api/loans/template`, `GET /api/loans/{loan_reference}/template?step=`).

**Why.** Fineract already knows the product's range, terms, default dates and amounts, and payment types, for this loan and this step. Before, the service read the loan product and worked out the rest on its own: for example, the approval and payout amount was the loan's principal. That is our own reading of Fineract's rules, and it can be wrong for a loan that was changed in Fineract. The template is Fineract's own answer, so the rules stay in one place. The partner gets the same rules from the form that the write uses.

What each step reads:

| Step | Fineract template | What we take from it |
|---|---|---|
| New client | `GET /clients/template` | The office id for `Kigali Main Office` (as before) |
| Loan application | `GET /loanproducts/external-id/PRD-SME-ASSET-FINANCE`, then `GET /loans/template?templateType=individual&productId=..` | The principal range and default, the term, rate, schedule and currency. The Fineract body is built from these. |
| Approval | `GET /loans/external-id/{ref}/template?templateType=approval` | The amount to approve (the applied principal) and the expected disbursement date |
| Disbursement | `.../transactions/template?command=disburse` | The amount (the approved principal) |
| Repayment | `.../transactions/template?command=repayment` | The currency step and the payment types |
| Payoff (`payoff_amount`, the payoff form) | `.../transactions/template?command=prepayLoan` | What clears the loan today |

What I learned about the templates on Fineract 1.15.0:

- **Templates ignore the loan's status.** An approval template answers `200` for a loan that is already approved. A disbursement template answers for a loan that is not approved yet. So the forms check the status themselves and say `available: false` with a reason. The writes still rely on our own status checks and on Fineract's refusal.
- **Fineract refuses an approval after the expected disbursement date** (`error.msg.loan.expecteddisbursal.should.be.on.or.after.approval.date`). The approval template still proposes today as the approval date, even when that is too late. So we check `approved_on` ourselves and answer `422` before we send anything. Shown on the live Fineract in `evidence/P2.5/templates.txt`, part 12.
- **A payout before the expected disbursement date is allowed.** A loan expected on 2026-10-20 was paid out on 2026-10-13 (part 10 of the same file).
- **The default payout date is today, not the template's date.** The disbursement template proposes the loan's expected disbursement date. Once that date has passed, using it would backdate the payout to a day when no money moved. So the default is today, the day the money really moves. A partner can still send an earlier `transaction_date`.
- **Payment types include Fineract's own.** The repayment template lists `Money Transfer` and also two system-defined types used for Fineract's own adjustments (refund and chargeback). We accept only the partner types. The others get `422`.

**The trade-off: more reads.** Each write now reads one template before it sends anything (two reads for an application: the product, then the template). That is one more call to Fineract per write, and one more thing that can fail. I did not measure the extra time. In return there is no copy of Fineract's rules to keep in step. A failed template read means nothing was sent, so the request ends `FAILED` and the partner can send it again. The money templates are read under the loan's lock, right before the write. So the amount we send is the amount Fineract proposed at that moment.

**A retry looks before it reads.** A retry of a `FAILED` request first looks up its earlier attempt in Fineract by our key. Only then does it read templates. An earlier attempt that landed late is ours and is finished at once, without a template read. Reading first would check the retry against a loan that may have moved on. If the lookup itself fails, we do not know what happened, so the request stays `FINERACT_PENDING` with `504` until recovery decides.

### Setup reads the templates too

**Chosen:** setup builds each Fineract form from Fineract's own template, like the partner writes do. The files in `fineract/configuration/` hold only our business choices, written as names or codes. They hold no Fineract numeric id. A name or code the template does not offer stops setup, and the message lists the allowed values.

**Why.** Before, `office.json` had `"parentId": 1` and `loan-product.json` had enum ids like `"repaymentFrequencyType": 2`. Those numbers are Fineract's, not ours. They only work because a fresh Fineract 1.15 happens to number things that way. Now the file says `"parent": "Head Office"` and `"repaymentFrequency": "MONTHS"`, and Fineract gives the number.

What each setup step reads:

| Step | Fineract read | What we take from it |
|---|---|---|
| Office | `GET /offices/external-id/OFF-KGL-MAIN`, then `GET /offices/{id}` | Whether our office exists, and its name, parent and opening date |
| | `GET /offices/template` | The parent's id, found by name in `allowedParents` |
| | `GET /offices/{parent id}` | The parent's opening date. The template lists parents without it. |
| Currency | `GET /currencies` | The selected codes and the codes Fineract offers |
| Role | `GET /permissions` | The permission codes that exist |
| API user | `GET /users/template` | The office id and the role id, found by name in `allowedOffices` and `availableRoles` |
| Loan product | `GET /loanproducts/template` | The id for each of our codes, three defaults, the strategies, the currencies, and the GL accounts allowed for each GL role |

Where each loan product field comes from:

| Source | Fields |
|---|---|
| Our config, plain values | name, short name, external id, description, currency code, decimals, amounts and their range, number of repayments, repayment every, interest rate and its range, strategy code, the GL account for each role (by ERP reference) |
| Our config, as a code the template turns into an id | `repaymentFrequency` (`MONTHS`), `interestRateFrequency` (`YEARS`), `amortization` (`EQUAL_INSTALLMENTS`), `interestType` (`DECLINING_BALANCE`), `interestCalculationPeriod` (`SAME_AS_REPAYMENT_PERIOD`) |
| The template's default | `daysInYearType`, `daysInMonthType`, `repaymentStartDateType` |
| Fixed in code, checked against the template | cash accounting (`CASH`), schedule type `CUMULATIVE`, one payout, no interest recalculation |

The strategy, the currency and every GL account are checked against the template's lists. A GL account must be in the list for its role (asset, income, expense or liability), so a mapping to the wrong account type stops setup before Fineract sees it. A unit test checks that the body built from the saved 1.15 template is exactly the body the old code built. So the product did not change.

- **Why `interestRateFrequency` stays in the config.** The template's default is "Per month". Our rate is 18% a year. Taking the default would make it 18% a month. The period of the rate is a business choice, so it is ours to write.
- **The three settings that left the config.** `daysInYearType`, `daysInMonthType` and `repaymentStartDateType` are now the template's defaults. On 1.15 these are Actual, Actual and the disbursement date, the same values the config had. We had no business reason to pick them. The risk is that a later Fineract version has other defaults and quietly builds another product. Two checks cover this. A live test builds the body from the running Fineract's template and compares it with the old body (GL ids left out). And a re-run compares these three, and the schedule type, with the existing product, and stops if they differ.
- **Fineract does not check an office's opening date.** Fineract 1.15 accepts an opening date in the future, or one before the parent office opened. *(Read in the source, `OfficeCommandFromApiJsonDeserializer` and `Office.fromJson`, not tried.)* Fineract does refuse a client activated before its office opened, so a wrong date would only fail later, on a client, far from its cause. *(Also read in the source.)* So setup refuses both before it creates the office. On a re-run, an office with our external id but another name, parent or opening date stops setup.
- **`/currencies` splits its answer in two.** `selectedCurrencyOptions` has the currencies already in use. `currencyOptions` has only the others. On the clean stack, 2 were selected and 162 offered, with no code in both. So a code is valid if it is in either list, and the error message lists both.

Shown on a clean stack in `evidence/P2.6/`: the first run created every step, the second run said `exists` for each and wrote nothing, and the office and product read back from Fineract match (`office-and-product.txt`).

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

## Duplicate protection

All of it is in Postgres. Nothing is kept only in memory.

- A request is saved as a row in `operations`, unique on (type, `external_reference`). It stores a hash of the request and, once the Fineract part is done, the exact answer. A repeat gets that answer back (`200`), a running one gets `202`, different data gets `409`.
- The hash covers the partner as well as the body. If another partner reuses a reference, it gets `409 IDEMPOTENCY_CONFLICT` and never sees someone else's answer. `GET /api/transactions` also shows a partner only its own requests.
- Only one live client creation per `client_uid`, one live approval per loan and one live disbursement per loan. These are partial unique indexes. A request that ended `FAILED` does not count, so it can be tried again.
- Writes to one loan go one at a time, under a Postgres advisory lock per loan. The probe showed why: 10 repayments at once on one loan, straight at Fineract, lost 5 to Fineract's own `409` after about 4 s. With the lock, the same test lands all of them, and it fails when the lock is removed.
- Our op key goes to Fineract as the transaction's `externalId`, and `<op key>:<attempt>` goes in the `Idempotency-Key` header.

Both `UNIQUE (op_type, external_reference)` and `UNIQUE (op_key)` exist. `INSERT ... ON CONFLICT` can name only one of them, so two identical requests at once could fail on the other one. The concurrency test found this. `begin()` now catches that error, which only comes after the first insert has committed, and reads the saved row in a new transaction.

### A second reference for a loan that is taken: 409, not 202

A second disbursement reference for a loan that is already paid out, or still being paid out, gets `409 LOAN_ALREADY_DISBURSED`. The same goes for approvals (`LOAN_ALREADY_APPROVED`) and for `client_uid` (`CLIENT_UID_TAKEN`).

- **Why not `202`:** `202` means "your request was accepted and is running". Here the new reference was not accepted and will never run. A partner polling it would wait for nothing.
- `details.holder_status` shows the status of the request that holds the loan, so the partner can tell "still running" from "done".
- The new reference is not saved. If the first request ends `FAILED`, the loan is free again, and the new reference can be sent.

### What Fineract 1.15.0 does, as seen in the probe

I tried each of these against the running Fineract on 2026-10-13, as the API user.

- **`Idempotency-Key`.** After a success, the same key returns the first answer with `x-served-from-cache: true`. Fineract ignores the body: the same key with another amount still returned the first answer and created nothing. The key is not tied to a loan either: a key used on loan 1 and sent for loan 2 returned loan 1's answer. While the first call is still running, the same key gets `425` after about 3 s. After an error, the same key replays the error and does not run again. So each attempt needs its own key, unique everywhere.
- **Unique `externalId`.** A second loan transaction with the same `externalId` gets `400 validation.msg.loan.transaction.externalId.value.must.be.unique`, even on another loan. Nothing is created. This is the real guarantee. The header is a convenient replay on top.
- **A second disbursement** of a single-tranche loan gets `403 error.msg.loan.disburse.amount.must.be.less.than.approved.principal`. Five at once with different keys gave exactly one success and four of these. So Fineract's loan state is a real last line of defence.
- **Parallel writes to one loan** lose some to `409 error.msg.platform.service.query.conflict` after Fineract's own 3 internal retries (about 4 s). Those were not applied. A retry with a new key worked.
- **A repayment on a closed loan is accepted.** The loan becomes `Overpaid` and all of the money is booked as overpayment. We refuse it ourselves (see below).
- **Fineract rounds** `1000.6` RWF to `1001` without an error. We refuse anything that is not a whole number.
- **Error text can hold internal ids**, for example "Transaction with identifier 1 does not exist". We never pass Fineract's text on.

### How keys are made

`Idempotency-Key = <type>:<external_reference>:<attempt>`, for example `DSB:FIN-2026-000001:1`. The transaction's `externalId` is the same value without the attempt number. The key is saved, as `attempts`, before the call. The attempt only goes up when a `FAILED` request is sent again with the same reference, and that attempt looks the transaction up before it sends anything. A key is never made from the request data alone, because two real repayments can have identical data.

The ERP has its own unique rule on the reference, and its own debits-equal-credits rule. It does not trust the caller.

## Retries and partial failure

- We never send a Fineract write again until a lookup by `externalId` shows it did not happen.
- A request is only marked `FAILED` when we know nothing happened in Fineract: it was not reachable, it clearly refused, it lost a race (`409`) and a lookup found nothing, or recovery's lookup found nothing.
- **An unclear answer is not a "no".** After a timeout, `5xx` or `425`, the transaction is looked up at once. If it is not there yet, Fineract may still be working on it, so the request stays `FINERACT_PENDING`. The partner gets `504`, and repeats get `202`. Recovery looks again once the request is 180 s old. That is longer than our 30 s Fineract timeout plus Fineract's own internal retries. Only that later lookup can decide `FAILED`.
- **A refusal on a retry is checked too.** If Fineract refuses a second attempt, or says our `externalId` is taken, the transaction is looked up under our key first. If it is there, it was our own earlier attempt, and the request is finished, not refused.
- **ERP retries** are always safe, because the ERP answers `200` for an entry it already has. Wait 1, 2, 4 ... up to 60 s, plus up to 25% random extra so senders do not retry in step. Stop after 8 attempts and mark it `NEEDS_RECONCILIATION`. A `429` waits for `Retry-After` and does not count. A `409` or `422` from the ERP goes to `NEEDS_RECONCILIATION` at once, because sending it again cannot help.
- Recovery counts its failed lookups in `recovery_lookups`, apart from `attempts`, which is the attempt number in Fineract's key. After 8 failed lookups the request goes to `NEEDS_RECONCILIATION`. A repeat of such a request gets `409 NEEDS_RECONCILIATION`, not `202`, because it will not move without a person.

### The outbox runs inside the service, with no queue

- **Chosen:** a background thread in each Gunicorn worker. It runs recovery and sends due outbox rows every 5 seconds, and `POST /admin/recover` runs the same pass on demand.
- Rows are claimed with `SELECT ... FOR UPDATE SKIP LOCKED` and a lease of 120 s, and the lease is renewed just before each send. A result is only saved if the row is still the one that was claimed. So two workers never send the same row twice. A test with two senders at once checks this.
- **Why no queue or separate worker:** the outbox table is already a durable queue, written in the same transaction as the Fineract result. A broker would need that outbox anyway, would still deliver some messages twice, and is one more thing to run. A separate worker process would be one more container that does exactly what the thread does.
- **The cost:** every worker polls the database every 5 seconds. That is fine at this size.

## Fineract settings (Part B)

All of these are made by the setup step (`python -m integration_service.setup`, the compose service `setup`). The settings are in `fineract/configuration/`. Each row was checked on a clean stack: see `evidence/P1/`.

| Setting | Choice | Why |
|---|---|---|
| Office | `Kigali Main Office` (external id `OFF-KGL-MAIN`), under the default Head Office *(assumption)* | Keeps Fineract's top office as it is. Fineract only shows a user the clients and loans in its own office and below, so the API user sits in this office too. |
| Currency | RWF, no decimals | RWF has no cents. Fineract already knows RWF with 0 decimals. Setup adds it to the organisation's list and keeps the others, because `PUT /currencies` replaces the whole list. The product sets 0 decimals too. |
| Loan product | "SME Asset Finance" (`SMEA`, external id `PRD-SME-ASSET-FINANCE`): 18% a year, 12 monthly payments, declining balance, equal payments, paid out once, RWF. Default amount 5,000,000 RWF (100,000 to 50,000,000). Strategy `mifos-standard-strategy`. | The values the brief suggests. Checked with `GET /loanproducts/1` in `evidence/P1/product.txt`. |
| Accounting | Cash-based (`CASH` in the product template) | Journal entries only on payout and repayment, which matches Part H. Reconciliation is one entry per payment. It is less realistic, because no interest is booked between payments. |
| GL accounts | All owned by the ERP and synced. Codes follow the usual pattern: 1xxx assets, 2xxx liabilities, 4xxx income, 5xxx expenses. | In 1.15.0, cash-based accounting needs 9 GL roles. The ERP seed has a DETAIL account of the right type for each one (table below). The optional charge-off and goodwill roles are left out, because the brief does not use them. |
| API user | `integration-api`, with its own role `Integration Service` (11 permissions, below) | Least privilege. The admin user is only used by the setup step. |

**The 9 GL roles**, as set on the product and checked against the ERP chart and `gl_sync`:

| Fineract role | GL type it must have | ERP account |
|---|---|---|
| `fundSourceAccountId` | ASSET or LIABILITY | GL-1100 Bank |
| `loanPortfolioAccountId` | ASSET | GL-1200 Loan Receivable |
| `transfersInSuspenseAccountId` | ASSET | GL-1300 Transfers in Suspense |
| `interestOnLoanAccountId` | INCOME | GL-4100 Interest Income |
| `incomeFromFeeAccountId` | INCOME | GL-4200 Fee Income |
| `incomeFromPenaltyAccountId` | INCOME | GL-4300 Penalty Income |
| `incomeFromRecoveryAccountId` | INCOME | GL-4400 Recovery Income |
| `writeOffAccountId` | EXPENSE | GL-5100 Loan Write-off |
| `overpaymentLiabilityAccountId` | LIABILITY | GL-2100 Overpayment Liability |

Fineract checks the GL type of each role, but not DETAIL or HEADER. We only map DETAIL accounts, because nothing should post to a heading.

### The API user's permissions

The role `Integration Service` has exactly these 11 permissions, and setup removes any others:

| Permission | Why the service needs it |
|---|---|
| `SYNC_GLACCOUNT` | `PUT` on the plugin (GL sync) |
| `READ_GLACCOUNT` | `GET` on the plugin, to look up a sync that had no clear answer |
| `CREATE_CLIENT`, `ACTIVATE_CLIENT`, `READ_CLIENT` | Part C: create a client, activate it, find it by external id |
| `CREATE_LOAN`, `APPROVE_LOAN`, `DISBURSE_LOAN`, `REPAYMENT_LOAN` | Part D: the loan steps |
| `READ_LOAN` | Find loans and their transactions by external id. In 1.15.0 this also covers loan transactions. There is no separate `READ_LOANTRANSACTION`. |
| `READ_LOANPRODUCT` | Find the loan product by its external id |

Checked on the clean stack (`evidence/P1/least-privilege.txt`): the user can read a GL mapping and the loan product, and gets `403` when it tries to create a GL account, create a user, change role permissions or create a loan product. The probe (2026-10-13) showed these 11 are enough for everything Parts C to H do: clients, loans, payouts, repayments, lookups by external id and the repayment and payoff templates. Two codes are missing on purpose: `ADJUST_LOAN`, only needed for reversals, which are not built, and `READ_JOURNALENTRY`, which nothing uses yet.

### Re-running setup

Each step looks first, then creates only what is missing. A second run on the clean stack printed `exists` for every step and sent nothing to Fineract (`evidence/P1/setup-second-run.txt`, and again after the template change in `evidence/P2.6/setup-second-run.txt`). Some rules:

- The office is found by its external id, `OFF-KGL-MAIN`. If it has another name, parent or opening date, setup stops. An office with our name but another external id also stops setup.
- The role's permissions are set back to exactly the list in `role.json`. An unknown permission code stops setup before anything is written.
- The API user's office and role are set back if they changed. Its password is never changed on a re-run. Fineract can't show it, so there is nothing to compare. Instead, setup logs in as the user once. A wrong password or a missing permission stops setup with a clear message.
- An existing loan product is never changed, because loans may already use it. If its key settings differ from the config, setup stops and lists the differences.
- Setup refuses to create the loan product unless all 9 GL accounts it needs are `SYNCED`.

### What I learned about Fineract 1.15.0 while setting it up

- **Two loan product fields have new names.** Older docs say `allowPartialPeriodInterestCalcualtion` (misspelled) and `canDefineEmiAmount`. 1.15.0 refuses both as unsupported and wants `allowPartialPeriodInterestCalculation` and `canDefineInstallmentAmount`. The first real run of setup on a clean stack failed on this, and I fixed it with a test.
- Some fields only work with the newer `advanced-payment-allocation-strategy`: `chargeOffBehaviour`, `supportedInterestRefundTypes` and `fixedLength`. Fineract also refuses `enableAccrualActivityPosting` with cash accounting. Setup never sends them. *(Read in the source, not tried.)*
- A new role needs a description as well as a name. *(Read in the source, not tried.)*
- The active password policy (read from Fineract's database) needs 12 to 50 characters, upper and lower case, a digit and a symbol, and no character twice in a row. So `change-me-...` passwords fail. The default in `.env.example` is `Change-Me-Fineract-Api-1`.
- Every body with a date or a number as text needs `"locale": "en"` (and `"dateFormat"` for dates). Setup sends them with the office and the loan product. *(Read in the source.)*
- External ids with a colon, like `DSB:FIN-2026-000001`, work in Fineract's external-id URLs without escaping.

## Journal entries (Part H)

- **Where the split comes from.** After Fineract says yes, the service reads the transaction back by its `externalId` (`GET /loans/external-id/{loan}/transactions/external-id/{op key}`). It uses Fineract's own `amount`, `principalPortion`, `interestPortion`, `overpaymentPortion`, `feeChargesPortion`, `penaltyChargesPortion` and `date`. It never uses the amount from the partner's request, and it never does its own interest maths.
- **The mapping** is a pure function, `mapping/journal.py`, with unit tests:
  - Payout: DR 1200 Loan Receivable / CR 1100 Bank.
  - Repayment: DR 1100 Bank (amount) / CR 1200 (principal) / CR 4100 Interest Income (interest) / CR 2100 Overpayment Liability (overpayment). Lines of zero are left out.
- **The accounts** come from the GL roles in `fineract/configuration/loan-product.json`, the same file setup used to create the product. So the ERP entry uses the same synced accounts Fineract posts to. The probe confirmed that Fineract's own GL entries use exactly these accounts and amounts. In D-1, the ERP totals per account equalled Fineract's GL for the loan after every stage.
- **Entry date:** Fineract's transaction date.
- **Overpayments.** Paying more than is owed on an `ACTIVE` loan is allowed. Fineract books the extra as `overpaymentPortion` and the entry credits 2100. A repayment on a loan that is not `ACTIVE` (for example Closed) is refused with `422`. Fineract would accept it and turn all of it into an overpayment, which is almost never what the partner meant.
- **Fees, penalties and anything odd** are not guessed. The entry is not built and the request goes to `NEEDS_RECONCILIATION`, with the reason. The Fineract success is still saved. The same happens for a reversed transaction, portions that do not add up, the wrong type or `externalId`, a currency other than RWF, or a fraction of a franc. The SME Asset Finance product has no fees, so this does not happen in normal use.
- **Reversals** are *planned*, not built. The plan: Fineract's undo, with `reversalExternalId` `<op key>:REV`, then a new ERP entry with the opposite lines, reference `<op key>:REV` and `reverses` set to the original. Nothing is deleted. The probe showed two things to know first. Undo needs `ADJUST_LOAN`, which the API user does not have, so it must be added to `role.json`. And an undone Fineract transaction keeps its `externalId` and shows `manuallyReversed: true`; no new transaction is made. Today the mapping refuses a reversed transaction, so it can never be posted as if it were live.

## Open questions (my assumptions)

- Does a plugin in `custom/` count as "changing or extending Fineract"? I think yes. It is Fineract's own way to extend it.
- Should a payout return 200 while the ERP entry is still pending, or 202 until it is done? I return 200 with `erp_status`, because the money has moved once Fineract says yes.
- How do partners log in? I use one API key per partner.
- What is `FIN-2026-000001`? I assume it is a partner's `external_reference`.
- Should reconciliation include reversed payments? I assume yes, matched to their opposite entries.

## What I chose not to build

No message queue, no Redis, no separate worker, no OAuth. No reversals yet, and no repair call for `NEEDS_RECONCILIATION`.

## Known weaknesses

To be filled in from what testing shows. So far:

- **GL sync only creates accounts.** If the ERP renames an account after it is synced, Fineract keeps the old name. The report shows a warning, but nothing fixes it.
- **The GL sync routes use the partner key.** Syncing the chart is an operator task. In production it should sit on an internal route.
- **`/admin/recover` takes a partner key.** Any partner key works for it today. It should have its own operator key.
- **Clients and loans are not tied to a partner.** Any partner key can read, apply for or pay into any client or loan. Only `GET /api/transactions` is limited to the partner's own requests.
- **No repair call.** A request in `NEEDS_RECONCILIATION` stays there. The state machine allows a repair to `COMPLETED`, but no endpoint does it yet.
- **A repayment finished by recovery** reads `outstanding_after` at recovery time, so it can include later repayments on the same loan. On the normal path it is read under the loan's lock right after the write.
- **Each Gunicorn worker polls** the database every 5 seconds, even when there is nothing to do.
- **Test data stays in Fineract.** The live tests and `make simulate-gl-sync-4x` add GL accounts with 8-digit codes starting with 7, and a few test users and roles. They never delete anything, so the shared Fineract grows. `make reset` clears it.
