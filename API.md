# API

> **Status:** the Fineract plugin API and the Mock ERP API are built and tested. In the Integration Service API, the GL sync routes are built and tested. The other Integration Service endpoints are *planned*.

## OpenAPI specs

Each API has an OpenAPI 3 spec. The spec is generated from the code, not written by hand.

| API | Served at | Copy in this repo |
|---|---|---|
| Fineract ERP integration (my plugin) | `/fineract-provider/glsync.json` and `.yaml` | `docs/openapi/fineract-erp-integration.yaml` |
| Integration Service | `/openapi/openapi.json`, Swagger UI at `/openapi/swagger` | `docs/openapi/integration-service.yaml` |
| Mock ERP | `/openapi/openapi.json`, Swagger UI at `/openapi/swagger` | `docs/openapi/mock-erp.yaml` |

For the plugin, the build reads the annotations on the Java classes and writes the spec. Fineract's own spec (`fineract.json`) only covers Fineract's built-in code, so the plugin makes its own.

For the Mock ERP and the Integration Service, flask-openapi3 builds the spec from the Pydantic models in the code. `make build` writes the repo copies from the built images.

## Fineract plugin: ERP integration

These endpoints are inside Fineract. Only the Integration Service calls them. GL accounts are never created in Fineract any other way.

All integration endpoints share one prefix: `/fineract-provider/api/v1/erp-integration/`. This keeps them apart from Fineract's core APIs, so access can be allowed by path. See [DECISIONS.md](DECISIONS.md).

Every call needs Basic auth and the header `Fineract-Platform-TenantId: default`.

### `PUT /fineract-provider/api/v1/erp-integration/accounts/{erpReference}`

Creates the GL account for one ERP account. If it already exists with the same data, it returns the existing one.

- **Permission:** `SYNC_GLACCOUNT`
- **Body:** the same fields as Fineract's own `POST /glaccounts`, plus `parentAccount`

| Field | Rule |
|---|---|
| `name` | Required. 1 to 200 characters. |
| `glCode` | Required. 1 to 45 characters. Must not be used by another GL account. |
| `type` | Required. `1` ASSET, `2` LIABILITY, `3` EQUITY, `4` INCOME or `5` EXPENSE. Nothing else. |
| `usage` | Required. `1` DETAIL (a sub-ledger that transactions post to) or `2` HEADER (a heading that groups accounts). |
| `manualEntriesAllowed` | Required. `true` or `false`. |
| `description` | Optional. Up to 500 characters. |
| `parentAccount` | Optional. The parent account's ERP reference. The parent must already be synced, must be a HEADER account, and must have the same type. |
| `tagId` | Optional. A Fineract tag for this account type. |
| `{erpReference}` in the path | 1 to 100 characters: letters, digits, `.`, `_`, `:` or `-`. |
| Anything else | Rejected with 400. That includes Fineract's own `parentId`. Use `parentAccount` instead. |

Why `type` and `usage` are checked by the plugin: Fineract's GL table has no database rule for them. Without this check, a bad value could only be caught by Fineract's own validator.

**Example request**

```json
{ "name": "Loan Receivable", "glCode": "1200", "type": 1, "usage": 1,
  "manualEntriesAllowed": false, "parentAccount": "GL-1000" }
```

**What you get back**

| Situation | Response |
|---|---|
| New reference, GL code is free | `200 {"erpReference":"GL-1200","glAccountId":7,"glCode":"1200","outcome":"CREATED","parentAccount":"GL-1000"}` |
| Same reference, same data | `200`, same body but `"outcome":"UNCHANGED"`. Nothing is written. Key order, spaces and empty fields do not count as different data. |
| Same reference, different data | `409 error.msg.glaccount.sync.payload.conflict`. Nothing is written. |
| New reference, GL code already used | `409 error.msg.glaccount.sync.code.conflict` |
| Parent not synced yet | `409 error.msg.glaccount.sync.parent.not.synced` |
| Parent is a DETAIL account | `409 error.msg.glaccount.sync.parent.not.header` |
| Parent has a different type | `409 error.msg.glaccount.sync.parent.type.mismatch` |
| Bad field (for example `type: 6`, missing `usage`, unknown field) | `400`. The error names the field. |
| User without `SYNC_GLACCOUNT` | `403 error.msg.not.authorized` |
| Same `Idempotency-Key` sent again | The first response again, with header `x-served-from-cache: true` |
| Same `Idempotency-Key` while the first call is still running | `425` |

### `GET /fineract-provider/api/v1/erp-integration/accounts/{erpReference}`

Shows which GL account an ERP account belongs to. Use it when a sync timed out and you do not know if it worked. Check first, instead of sending again.

- **Permission:** `READ_GLACCOUNT`
- **200:** `{"erpReference":"GL-1200","glAccountId":7,"glCode":"1200","name":"Loan Receivable","type":"ASSET","usage":"DETAIL","parentAccount":"GL-1000","hierarchy":".3.","syncedAt":"2026-10-12T14:36:58Z"}`
- **404:** the account has not been synced yet

`hierarchy` is Fineract's own path field. A top-level account has `.`. A child adds its own GL id to its parent's path.

## Integration Service

### General rules

- **Login:** each partner sends `X-API-Key`. A missing or wrong key gets `401`.
- **Tracing:** send `X-Correlation-ID`, or one is made for you. It comes back on every response and appears in every log line.
- **IDs:** you use `client_uid` (like `SME-000001`), `loan_reference` (like `LN-2026-000001`, we issue it) and `external_reference`. You never see or send Fineract's numeric IDs.
- **Money:** Rwandan francs (RWF), whole numbers only. RWF has no cents.

### The `external_reference` rule

- Every request that changes something must have one. Without it you get `422`.
- Make it **once**, when you decide to do the business action. Save it. Send the **same value** on every retry. Never build it from a timestamp, and never make a new one for each HTTP attempt.
- Use one reference per business action. Two real repayments of the same amount on the same day need two different references.
- At most 40 characters: letters, digits, `.`, `_`, `:` or `-`. Longer gets `422`.
- `X-Correlation-ID` is different. It only helps trace a request, and it may change on every retry.

### Sending the same request twice

| Situation | Response |
|---|---|
| New reference | Done once. `201` or `200`. |
| Same reference, same data, already finished | `200` with the exact same body as the first time |
| Same reference, same data, still running | `202` with the current status. It is never run twice. |
| Same reference, different data | `409 IDEMPOTENCY_CONFLICT` |

"Finished" means the Fineract part is done. A disbursement whose journal entry is still on its way to the ERP is finished. To see its latest status, call `GET /api/transactions/{external_reference}`.

| Action | What must be unique |
|---|---|
| GL sync | The reference, and the ERP account code |
| New client | The reference, and `client_uid` (reusing a `client_uid` gets `409`) |
| Loan application | The reference |
| Approval | The reference. A loan is approved once. |
| Disbursement | The reference. A loan is paid out once, even if different references are used. |
| Repayment | The reference. A loan can have many repayments. |

### Error format

```json
{ "error": { "code": "IDEMPOTENCY_CONFLICT",
             "message": "external_reference DSB-0001 was already used with different data",
             "correlation_id": "6f1c…", "details": {} } }
```

| HTTP | Example codes |
|---|---|
| 400, 422 | `VALIDATION_ERROR` |
| 401 | `UNAUTHENTICATED` |
| 404 | `NOT_FOUND` |
| 409 | `IDEMPOTENCY_CONFLICT`, `CLIENT_UID_TAKEN`, `LOAN_ALREADY_DISBURSED`, `GL_CODE_TAKEN` |
| 502, 503, 504 | `FINERACT_UNAVAILABLE`, `FINERACT_TIMEOUT` (we are still finding out what happened), `ERP_UNAVAILABLE`, `ERP_TIMEOUT` |
| 500 | `INTERNAL_ERROR`. The message does not show the cause. The full error is in the log, under the same correlation id. |

### Endpoints

| Call | What it does |
|---|---|
| `POST /api/accounts/sync` | Reads the ERP chart of accounts and syncs each account to Fineract. Safe to run again. Built, see below. |
| `GET /api/accounts/sync` | The sync state of every account. Calls nothing. Built. |
| `POST /api/accounts/{erp_reference}/sync` | Syncs one ERP account. Built. |
| `POST /api/clients` *(planned, like every row below)* | Creates a client |
| `GET /api/clients/{client_uid}` | Finds a client and checks it exists, is active and has the right name |
| `POST /api/loans` | Applies for a loan. Returns the new `loan_reference`. |
| `POST /api/loans/{loan_reference}/approve` | Approves the loan, once |
| `GET /api/loans/{loan_reference}` | Loan status and balances, as Fineract reports them |
| `POST /api/loans/{loan_reference}/disburse` | Pays out the loan, once |
| `POST /api/loans/{loan_reference}/repayments` | Records a repayment |
| `GET /api/transactions/{external_reference}` | Status and history of one request. This answers "what happened to X?". |
| `POST /api/reconciliation` | Compares Fineract and the ERP. Read only. |
| `POST /admin/faults` | Turns on fake failures, for testing. Not for partners. |
| `POST /admin/recover` | Re-checks unfinished requests now. Not for partners. |

### GL sync

These are the only Integration Service endpoints built so far. They need a partner `X-API-Key` like every `/api` route. In production they would sit on an internal route, because syncing the chart is an operator task.

They take no request body. The data comes from the ERP, not from the caller. So "same reference, different data" can't happen here. Each account is keyed by its own ERP `external_reference`: one `gl_sync` row, one plugin mapping, and its own `Idempotency-Key`.

#### `POST /api/accounts/sync`

Syncs the whole ERP chart, parents first, and answers with the state of every account.

| Situation | Response |
|---|---|
| The sync ran | `200` with the report |
| Another sync is running | `202` with the current report and `"in_progress": true`. Nothing is sent. |
| The ERP can't be reached, or answers `503` | `503 ERP_UNAVAILABLE` |
| The ERP times out | `504 ERP_TIMEOUT` |
| Any other ERP failure | `502 ERP_UNAVAILABLE` |

A Fineract failure never fails the call. It shows on the account: `SYNC_PENDING` when Fineract could not be reached or the answer was unclear, `FAILED` with a reason when Fineract refused.

Real answer on the clean stack, after setup had synced the chart (`evidence/P1/service.txt`, shortened to 2 of 15 accounts):

```json
{
  "accounts": [
    {"attempts": 1, "code": "1000", "erp_reference": "GL-1000", "last_error": null, "name": "Assets", "status": "SYNCED"},
    {"attempts": 1, "code": "1200", "erp_reference": "GL-1200", "last_error": null, "name": "Loan Receivable", "status": "SYNCED"}
  ],
  "counts": {"FAILED": 0, "SYNCED": 15, "SYNC_PENDING": 0},
  "in_progress": false,
  "writes": 0
}
```

| Field | Meaning |
|---|---|
| `status` | `SYNC_PENDING`, `SYNCED` or `FAILED` |
| `attempts` | How many times this account was sent to Fineract |
| `last_error` | Why it is not synced, in our own words (with a Fineract error code when there is no text for it), for example `GL_CODE_TAKEN: GL code X is already used by another GL account in Fineract`. On a `SYNCED` account it is a warning: `changed in ERP; updates are out of scope`. Never Fineract's own text, which can hold its ids. |
| `writes` | How many accounts this call sent to Fineract. `0` means everything was already synced. |
| `in_progress` | `true` when another sync was running, so this call did nothing |

No Fineract id appears in the answer.

#### `GET /api/accounts/sync`

The same report, read from our database. It calls neither the ERP nor Fineract. `writes` is `0`.

#### `POST /api/accounts/{erp_reference}/sync`

Syncs one ERP account, for example `GL-1200`.

| Situation | Response |
|---|---|
| Done | `200 {"account": {...one account, as above...}, "in_progress": false}` |
| Another sync is running | `202`, `account` is its current state, or `null` if it was never recorded |
| The ERP has no account with that reference | `404 NOT_FOUND` |
| ERP failures | Same as above (`502`, `503`, `504`) |

Real answers on the clean stack:

```
POST /api/accounts/GL-1200/sync -> 200
{"account":{"attempts":1,"code":"1200","erp_reference":"GL-1200","last_error":null,"name":"Loan Receivable","status":"SYNCED"},"in_progress":false}

POST /api/accounts/GL-9999/sync -> 404
{"error":{"code":"NOT_FOUND","correlation_id":"c-19c7…","details":{},"message":"The ERP has no account GL-9999."}}
```

## Mock ERP

The ERP that owns the chart of accounts and the journal. Only the Integration Service calls it. It runs at http://localhost:8090.

### General rules

- **Login:** every call to `/api` and `/admin` needs the header `X-API-Key`. The key is `ERP_API_KEY` in `.env`. A missing or wrong key gets `401`. `/health`, `/openapi` and `/ui` need no key. They are only open on `localhost`.
- **Tracing:** send `X-Correlation-ID`, or one is made for you (like `c-fa059c48…`). It comes back on every response. It is in the log line, and it is saved with each journal entry.
- **Money:** whole RWF only. A decimal or a string amount gets `422`.
- **References:** `external_reference` is 1 to 64 characters: letters, digits, `.`, `_`, `:` or `-`. It must start with a letter or a digit.

### Sending the same request twice

This works the same way for accounts and for journal entries. The ERP keeps a hash of each request.

| Situation | Response |
|---|---|
| New reference | Saved. `201` with `"status": "CREATED"`. |
| Same reference, same data | Nothing new is saved. `200` with `"status": "ALREADY_EXISTS"` and the stored record. |
| Same reference, different data | `409 reference_conflict`. Nothing is saved. |

For journal entries, the order of the lines and of the JSON keys does not matter. The rules are checked first. So a broken retry still gets `422`, even if its reference is already stored.

### Error format

Every error has this shape. This is a real answer from the running ERP:

```json
{ "error": { "code": "not_found",
             "message": "No journal entry NOPE.",
             "details": { "external_reference": "NOPE" },
             "correlation_id": "c-f031a849ad294968b2b1b861605a4aa8" } }
```

| HTTP | Code | When |
|---|---|---|
| 401 | `unauthorized` | Missing or wrong `X-API-Key` |
| 404 | `not_found` | Unknown account code or journal reference |
| 409 | `reference_conflict` | The reference is already stored with different data |
| 409 | `code_taken` | A new account uses a code that another reference already has |
| 422 | `invalid_request` | The body or query does not match the model. `details.problems` lists each field. Also a `reverses` that points at no stored entry. |
| 422 | `entry_not_balanced` | Debits do not equal credits |
| 422 | `unknown_account` | A line uses an account code that does not exist |
| 422 | `header_account` | A line uses a HEADER account. Only DETAIL accounts take lines. |
| 422 | `parent_invalid` | The parent account is missing, is not a HEADER, or has another type. Or a DETAIL account has no parent. |
| 429 | `rate_limited` | Fault switch. Has a `Retry-After` header. |
| 503 | `erp_unavailable` | Fault switch: the ERP is "down" |
| 504 | `timeout` | Fault switch: the ERP "timed out". Nothing was saved. |

The generated spec lists this same format for `401`, `422`, and for the fault answers `429`, `503` and `504`.

### Endpoints

| Call | What it does |
|---|---|
| `POST /api/accounts` | Adds an account to the chart. Safe to repeat. |
| `GET /api/accounts` | Lists accounts, each HEADER followed by its children. `?usage=HEADER` or `?usage=DETAIL` filters. |
| `GET /api/accounts/{code}/balance` | Debit total, credit total and balance. A HEADER adds up every account under it. |
| `POST /api/journal-entries` | Saves a journal entry. Safe to repeat. |
| `GET /api/journal-entries/{external_reference}` | One journal entry |
| `GET /api/journal-entries` | Searches entries, newest first. Filters below. |
| `GET /admin/faults` | Shows the current fault setting |
| `PUT /admin/faults` | Turns a fault on |
| `DELETE /admin/faults` | Turns the fault off |
| `GET /health` | `200 {"status":"UP","database":"ok"}`, or `503` if the database can't be reached |

### `POST /api/accounts`

| Field | Rule |
|---|---|
| `code` | Required. 4 to 10 digits. |
| `external_reference` | Required. See the reference rule above. |
| `name` | Required. 1 to 200 characters. |
| `type` | Required. `ASSET`, `LIABILITY`, `EQUITY`, `INCOME` or `EXPENSE`. |
| `usage` | Required. `HEADER` (groups accounts) or `DETAIL` (takes journal lines). |
| `parent_code` | A HEADER account of the same type. Required for a DETAIL account. |
| `currency` | Optional. Only `RWF`. |
| Anything else | `422` |

```json
{ "code": "1400", "external_reference": "GL-1400", "name": "Mobile Money",
  "type": "ASSET", "usage": "DETAIL", "parent_code": "1000" }
```

`201`. Sending it again gives `200` with the same body and `"status": "ALREADY_EXISTS"`.

```json
{ "code": "1400", "external_reference": "GL-1400", "name": "Mobile Money",
  "type": "ASSET", "usage": "DETAIL", "parent_code": "1000", "currency": "RWF",
  "created_at": "2026-10-13T10:02:56.487958Z", "status": "CREATED" }
```

The starting chart has 4 HEADER accounts (`1000` Assets, `2000` Liabilities, `4000` Income, `5000` Expenses) and 9 DETAIL accounts under them, such as `1100` Bank, `1200` Loan Receivable and `4100` Interest Income. Each has the reference `GL-<code>`.

### `GET /api/accounts/{code}/balance`

After the repayment example below, the Assets heading shows:

```json
{ "code": "1000", "name": "Assets", "type": "ASSET", "usage": "HEADER",
  "debit_total": 458400, "credit_total": 383400, "balance": 75000, "normal_side": "DEBIT" }
```

`balance` is the normal side minus the other side. ASSET and EXPENSE accounts are normally debit. The others are normally credit.

### `POST /api/journal-entries`

| Field | Rule |
|---|---|
| `external_reference` | Required. See the reference rule above. |
| `entry_date` | Required. A date, like `2026-10-12`. |
| `source` | Required. `DISBURSEMENT`, `REPAYMENT`, `REVERSAL` or `MANUAL`. |
| `description` | Optional. Up to 500 characters. |
| `reverses` | Only for a `REVERSAL`, and then required. The reference of a stored entry. |
| `lines` | Required. 2 to 50 lines. Each has `account_code` and exactly one of `debit` or `credit` above zero. Debits must equal credits. |
| Anything else | `422` |

```json
{ "external_reference": "FIN-2026-000002", "entry_date": "2026-10-12",
  "source": "REPAYMENT", "description": "Repayment LN-2026-000001",
  "lines": [ { "account_code": "1100", "debit": 458400 },
             { "account_code": "1200", "credit": 383400 },
             { "account_code": "4100", "credit": 75000 } ] }
```

`201`. A repeat of the same entry gets `200` with the same body and `"status": "ALREADY_EXISTS"`:

```json
{ "erp_transaction_id": "3476f214-afac-4e31-907a-e519cfe567a8",
  "external_reference": "FIN-2026-000002", "entry_date": "2026-10-12",
  "source": "REPAYMENT", "description": "Repayment LN-2026-000001", "reverses": null,
  "total": 458400,
  "lines": [ { "account_code": "1100", "account_name": "Bank", "debit": 458400, "credit": 0 },
             { "account_code": "1200", "account_name": "Loan Receivable", "debit": 0, "credit": 383400 },
             { "account_code": "4100", "account_name": "Interest Income", "debit": 0, "credit": 75000 } ],
  "created_at": "2026-10-13T10:02:56.497918Z", "correlation_id": "c-docs-1",
  "status": "CREATED" }
```

`erp_transaction_id` is the ERP's own id for the entry. `total` is the sum of the debits. `correlation_id` is the one from the call that first saved the entry. A repeat gets the stored one back.

Two errors from the same run:

```json
{ "error": { "code": "entry_not_balanced", "message": "Debits are 1000 and credits are 900.",
             "details": { "debits": 1000, "credits": 900 }, "correlation_id": "c-4d313c5e…" } }
```

```json
{ "error": { "code": "invalid_request", "message": "lines.0.debit: Input should be a valid integer",
             "details": { "problems": [
               { "field": "lines.0.debit", "problem": "Input should be a valid integer" },
               { "field": "lines.1.credit", "problem": "Input should be a valid integer" } ] },
             "correlation_id": "c-ec282a4f…" } }
```

`GET /api/journal-entries/{external_reference}` returns the same body, without `status`.

### `GET /api/journal-entries`

Returns `{"items": [...]}`, newest first. Every filter is optional, and they all must match.

| Query | What it does |
|---|---|
| `from`, `to` | First and last `entry_date` |
| `reference` | An exact reference. Repeat it to fetch many at once, up to 1000. Reconciliation uses this. |
| `source` | One source |
| `q` | Text found in the reference or the description |
| `limit` | 1 to 1000. Default 500. |

### The fault switch: `/admin/faults`

`PUT` turns a fault on and replaces any earlier one:

| Field | Rule |
|---|---|
| `mode` | Required. `down`, `timeout`, `timeout_after_write` or `rate_limited`. |
| `path_prefix` | Which paths fail. Must start with `/api`. Default `/api`. |
| `delay_seconds` | How long the timeout modes wait. 1 to 120. Default: `ERP_TIMEOUT_DELAY` from `.env`. |
| `remaining` | How many calls fail. Then the fault turns itself off. Leave it out to fail until `DELETE`. |
| `retry_after` | The `Retry-After` value for `rate_limited`, in seconds. Default 5. |

```json
{ "mode": "timeout_after_write", "delay_seconds": 2, "remaining": 1 }
```

`GET`, `PUT` and `DELETE` all return the current setting:

```json
{ "mode": "timeout_after_write", "path_prefix": "/api", "delay_seconds": 2,
  "remaining": 1, "retry_after": 5, "active": true }
```

What each mode does is in [ARCHITECTURE.md](ARCHITECTURE.md#the-mock-erp). The key is checked before the fault switch. So a call without a key gets `401` and does not use up a fault.
