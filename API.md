# API

> **Status:** every endpoint below is built and tested. The examples come from real runs on the local stack, on 2026-10-13 and 2026-10-14. Where an example comes from a simulation, the simulation is named, and its `make` command in [SIMULATIONS.md](SIMULATIONS.md) reproduces it. A few examples come from earlier runs with no `make` command. They are marked as such.

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

Each request is saved in Postgres under its type and `external_reference` (for example `DSB:FIN-2026-000001`), with a hash of the body and of the partner. The answer is saved too. Nothing is kept only in memory.

| Situation | Response |
|---|---|
| New reference | Done once. `201` for a new client or loan application, `200` for approve, disburse and repay. |
| Same reference, same data, already finished | `200` with the exact body saved the first time. This is `200` even when the first answer was `201`. |
| Same reference, same data, still running | `202` with the current status. It is never run twice. |
| Same reference, different data | `409 IDEMPOTENCY_CONFLICT`. Nothing is sent. |
| Same reference sent by another partner | `409 IDEMPOTENCY_CONFLICT`. A partner never sees another partner's answer. |
| Same reference, it earlier ended `FAILED` | It runs again under the same reference. Fineract gets a new attempt number in its `Idempotency-Key`, and the transaction is looked up first. |
| Same reference, it waits for a manual check (`NEEDS_RECONCILIATION`, no saved answer) | `409 NEEDS_RECONCILIATION` with `details.status` and `details.type`. It will not run again by itself. |

"Finished" means the Fineract part is done. A disbursement or repayment answers `200` while its journal entry is still on its way to the ERP. The answer has `erp_status`:

| `erp_status` | Meaning |
|---|---|
| `PENDING` | The journal entry is saved in our outbox and waits to be sent |
| `SENT` | The ERP saved it |
| `NEEDS_RECONCILIATION` | It cannot be sent without a person: the entry could not be built without guessing, the ERP refused it, or it failed 8 times |

`status` and `erp_status` in a saved answer are the values from the first answer. A replay returns that same body. The latest values are in `GET /api/transactions/{external_reference}`.

The `202` body:

```json
{ "external_reference": "DSB-0001", "type": "DISBURSEMENT", "status": "FINERACT_PENDING",
  "client_uid": "SME-000001", "loan_reference": "LN-2026-000001",
  "message": "This request is still running and will not run twice. See GET /api/transactions/DSB-0001." }
```

**What must be unique.** Some actions are unique for more than the reference:

| Action | Type | What must be unique | A different reference for the same thing |
|---|---|---|---|
| New client | `CLI` | The reference, and `client_uid` | `409 CLIENT_UID_TAKEN` |
| Loan application | `APP` | The reference | A new loan |
| Approval | `APR` | The reference. A loan is approved once. | `409 LOAN_ALREADY_APPROVED` |
| Disbursement | `DSB` | The reference. A loan is paid out once. | `409 LOAN_ALREADY_DISBURSED` |
| Repayment | `RPY` | The reference. A loan can have many repayments. | A new repayment |

A second reference gets `409` even while the first one is still running. `details.holder_status` shows the status of the first one. The second reference is not saved, so it can be used again if the first one ends `FAILED`. See [DECISIONS.md](DECISIONS.md) for why this is `409` and not `202`.

### When Fineract fails or gives no clear answer

We never send a request to Fineract a second time before we check, by its `externalId`, whether the first one worked.

| What happened | Our status | Answer | What you do |
|---|---|---|---|
| Fineract could not be reached. Nothing was sent. | `FAILED` | `503 FINERACT_UNAVAILABLE` | Send the same request again |
| Fineract lost a race with another write (its `409`), and a lookup finds nothing | `FAILED` | `503 FINERACT_UNAVAILABLE` | Send the same request again |
| Another write to the same loan held its lock for 30 s. Nothing was sent. | `FAILED` | `503 FINERACT_UNAVAILABLE` | Send the same request again |
| Timeout, `5xx` or `425`, and a lookup right after finds the transaction | Done | `200` as normal | Nothing |
| Timeout, `5xx` or `425`, and the lookup finds nothing yet or fails | `FINERACT_PENDING` | `504 FINERACT_TIMEOUT` | Wait. A repeat gets `202`. Recovery looks again after about 3 minutes and finishes it, or marks it `FAILED` if Fineract has no record of it. |
| Fineract said yes, but its transaction could not be read back | `FINERACT_PENDING` | `202` | Wait. The next recovery pass finishes it from Fineract's data. |
| Fineract refused (a clear `4xx`) | `FAILED` | `409` or `422` in our words | Fix the request, or use the answer |
| Fineract refused our own login or permissions | `FAILED` | `502 FINERACT_UNAVAILABLE` | Nothing. It is our problem. |
| A Fineract template we need before the write could not be read. Nothing was sent. | `FAILED` | `503 FINERACT_UNAVAILABLE` (for an application or an approval: `504` if the read timed out, `502` for another read error) | Send the same request again |
| A retry, and the lookup of the earlier attempt fails | `FINERACT_PENDING` | `504 FINERACT_TIMEOUT` | Wait. Recovery looks again. |

On a retry, a refusal is checked once more: if Fineract already holds a transaction under our key, it was our own earlier attempt, so it is finished, not refused.

**A retry looks first, then reads the templates.** When a `FAILED` request is sent again (attempt 2 or later), the service first looks up the earlier attempt in Fineract by our key. Only when it is not there does the service read the templates and check the request. So an earlier attempt that landed late is found and finished. It is not checked again against a template, which may no longer fit a loan that has moved on. If that lookup fails, we can't tell what happened: the request stays `FINERACT_PENDING` and gets `504`.

For a template read failure, the message says so in our own words:

```
503 {"error": {"code": "FINERACT_UNAVAILABLE", "message": "Could not read Fineract's disbursement template. Nothing was sent. Send the same request again.", ...}}
```

This text is from the code and its tests. It was not run on the live stack.

We never pass Fineract's error text on, because it can hold Fineract's internal ids. When Fineract refuses with a code we have not mapped, the answer is `422 VALIDATION_ERROR` with Fineract's error codes in `details.fineract_codes`.

### Error format

Every error has this shape. A real answer, from C-1 (`make demo-identity`):

```json
{ "error": { "code": "IDEMPOTENCY_CONFLICT", "correlation_id": "c-demo-54e587-008", "details": {},
             "message": "external_reference C1-CLI-54E587-edf5 was already used with different data." } }
```

A `422 VALIDATION_ERROR` for a field names the field and the problem in `message`, as `field: problem`. `details.problems` lists every problem found. The same shape is used for a bad body (Pydantic) and for a rule taken from a Fineract template. A real answer, from a run of the form endpoints on 2026-10-13:

```json
{ "error": { "code": "VALIDATION_ERROR", "correlation_id": "c-docs-…",
             "details": {"problems": [{"field": "principal", "problem": "must be between 100000 and 50000000 RWF (the product range)"}]},
             "message": "principal: must be between 100000 and 50000000 RWF (the product range)" } }
```

These are all the codes in `errors.py`:

| HTTP | Code | When |
|---|---|---|
| 401 | `UNAUTHENTICATED` | Missing or wrong `X-API-Key` |
| 404 | `NOT_FOUND` | Unknown path, unknown `external_reference` in `GET /api/transactions`, or an ERP account unknown to the ERP (GL sync) |
| 404 | `CLIENT_NOT_FOUND` | No such client in Fineract, or a loan application for a `client_uid` we did not create |
| 404 | `LOAN_NOT_FOUND` | No loan with that `loan_reference` |
| 405, 422 | `VALIDATION_ERROR` | The body, path or query does not match (`details.problems` lists each field), a rule from a Fineract template (principal range, payment type), a date rule, a loan that is not `ACTIVE` for a repayment (`details.loan_status`), or a Fineract refusal (`details.fineract_codes` when we have no mapping) |
| 409 | `IDEMPOTENCY_CONFLICT` | The reference was used with different data, or by another partner, or Fineract already holds another transaction under it |
| 409 | `CLIENT_UID_TAKEN` | The `client_uid` belongs to another reference, or Fineract already has a client with it |
| 409 | `LOAN_ALREADY_APPROVED` | Another reference approved, or is approving, this loan, or Fineract shows it approved |
| 409 | `LOAN_ALREADY_DISBURSED` | Another reference paid out, or is paying out, this loan, or Fineract refuses because it is already paid out |
| 409 | `NEEDS_RECONCILIATION` | The request waits for a manual check and will not run again by itself |
| 409 | `GL_CODE_TAKEN` | GL sync only: the GL code is used by another GL account in Fineract (shown in `last_error`) |
| 502, 503 | `FINERACT_UNAVAILABLE` | See the table above |
| 504 | `FINERACT_TIMEOUT` | We are still finding out what happened. See the table above. |
| 502, 503 | `ERP_UNAVAILABLE` | GL sync and reconciliation only: the ERP could not be read |
| 504 | `ERP_TIMEOUT` | GL sync and reconciliation only: the ERP did not answer |
| 500 | `INTERNAL_ERROR` | A bug. The message does not show the cause. The full error is in the log, under the same correlation id. |

A disbursement or repayment never fails because of the ERP. The ERP part runs later, from the outbox.

### Endpoints

| Call | What it does | Status |
|---|---|---|
| `POST /api/accounts/sync` | Reads the ERP chart of accounts and syncs each account to Fineract | Built |
| `GET /api/accounts/sync` | The sync state of every account. Calls nothing. | Built |
| `POST /api/accounts/{erp_reference}/sync` | Syncs one ERP account | Built |
| `POST /api/clients` | Creates an active client | Built |
| `GET /api/clients/{client_uid}` | Checks that a client exists, is active and has the right name | Built |
| `GET /api/clients/template` | The client form: fields, rules, office and activation date | Built |
| `GET /api/loans/template` | The loan application form: the product's range and terms. `?client_uid=` adds that client. | Built |
| `GET /api/loans/{loan_reference}/template?step=` | The form for the next step of one loan: `approval`, `disbursement`, `repayment` or `payoff` | Built |
| `POST /api/loans` | Applies for a loan. Returns the new `loan_reference`. | Built |
| `POST /api/loans/{loan_reference}/approve` | Approves the loan for the amount Fineract proposes, once | Built |
| `GET /api/loans/{loan_reference}` | Loan status and balances, as Fineract reports them | Built |
| `POST /api/loans/{loan_reference}/disburse` | Pays out the approved amount, once | Built |
| `POST /api/loans/{loan_reference}/repayments` | Records a repayment | Built |
| `GET /api/transactions/{external_reference}` | Every request with that reference: type, status, history and `erp_status`. This answers "what happened to X?". | Built |
| `POST /admin/recover` | Runs one recovery pass now. Not for partners. | Built |
| `POST /api/reconciliation` | Compares Fineract and the ERP. Read only. | Built |

Fake failures for testing are switched on in the Mock ERP (`/admin/faults`, below) and in the fault proxy in front of Fineract (see [ARCHITECTURE.md](ARCHITECTURE.md)), not here.

### GL sync

These need a partner `X-API-Key` like every `/api` route. In production they would sit on an internal route, because syncing the chart is an operator task.

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

An answer from an earlier run on a clean stack, after setup had synced the chart (output not kept; shortened to 2 of 15 accounts):

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

Answers from an earlier run on a clean stack (output not kept):

```
POST /api/accounts/GL-1200/sync -> 200
{"account":{"attempts":1,"code":"1200","erp_reference":"GL-1200","last_error":null,"name":"Loan Receivable","status":"SYNCED"},"in_progress":false}

POST /api/accounts/GL-9999/sync -> 404
{"error":{"code":"NOT_FOUND","correlation_id":"c-19c7…","details":{},"message":"The ERP has no account GL-9999."}}
```

### Clients (Part C)

#### `POST /api/clients`

Creates the client in Fineract and makes it active, in one call. Fineract stores `client_uid` as the client's `externalId`, in the office `Kigali Main Office`.

| Field | Rule |
|---|---|
| `external_reference` | Required. See the rule above. |
| `client_uid` | Required. 2 to 10 capital letters, a dash and 6 digits, like `SME-000001`. Unique. |
| `first_name`, `last_name` | Required. 1 to 50 characters each: letters, joined by single spaces, `'`, `.` or `-`. |
| Anything else | `422` |

Real run, from C-1 (`make demo-identity`):

```
POST /api/clients {"client_uid": "DEMO-797009", "external_reference": "C1-CLI-54E587-edf5", "first_name": "Test", "last_name": "Client One"}
-> 201 {"client_uid": "DEMO-797009", "external_reference": "C1-CLI-54E587-edf5", "name": "Test Client One", "status": "ACTIVE"}

the same request again
-> 200, the same body

a new reference with the same client_uid
-> 409 {"error": {"code": "CLIENT_UID_TAKEN", "correlation_id": "c-demo-54e587-011", "details": {}, "message": "client_uid DEMO-797009 is already used by another external_reference."}}
```

#### `GET /api/clients/{client_uid}`

Reads the client from Fineract right now. Add `?name=` to check the name. Case and spaces do not count. It saves nothing.

```
GET /api/clients/DEMO-797009?name=Test Client One
-> 200 {"active": true, "client_uid": "DEMO-797009", "matches_our_record": true, "name": "Test Client One", "name_matches": true, "status": "ACTIVE"}
```

`matches_our_record` is true when we created this client, Fineract holds the same name, and the client is active. `name_matches` is `null` without `?name=`. An unknown client gives `404 CLIENT_NOT_FOUND`.

### Loans (Parts D and E)

#### `POST /api/loans`

Applies for a loan on the SME Asset Finance product. The principal range, term, rate and schedule come from Fineract's loan template for the product (`GET /api/loans/template` shows them). We issue the `loan_reference` (`LN-<year>-<6 digits>`, from a Postgres sequence). It is the loan's `externalId` in Fineract.

| Field | Rule |
|---|---|
| `external_reference` | Required |
| `client_uid` | Required. A client created through `POST /api/clients`, else `404 CLIENT_NOT_FOUND`. |
| `principal` | Required. Whole RWF, inside the range in Fineract's loan template (100,000 to 50,000,000 today), else `422`. |
| `expected_disbursement_date` | Optional, `YYYY-MM-DD`, today or later in Kigali. Default today. |

A principal outside the range is refused after the template is read. The request is saved as `FAILED`, and no loan is sent to Fineract. A real answer, from a run of the form endpoints on 2026-10-13:

```
POST /api/loans {"client_uid": "TPL-000001", "external_reference": "P25-APP-TPL-BIG", "principal": 60000000}
-> 422 {"error": {"code": "VALIDATION_ERROR", "details": {"problems": [{"field": "principal", "problem": "must be between 100000 and 50000000 RWF (the product range)"}]}, "message": "principal: must be between 100000 and 50000000 RWF (the product range)", ...}}
```

Real run, from D-1 (`make demo-lifecycle`):

```
POST /api/loans {"client_uid": "DEMO-371457", "external_reference": "APP-B63B2F-bd7f", "principal": 5000000}
-> 201 {"client_uid": "DEMO-371457", "expected_disbursement_date": "2026-10-13", "external_reference": "APP-B63B2F-bd7f", "loan_reference": "LN-2026-000001", "principal": 5000000, "status": "SUBMITTED_AND_PENDING_APPROVAL"}
```

#### `POST /api/loans/{loan_reference}/approve`

Approves the loan for the amount in Fineract's approval template. That is the applied principal. There is no partial approval, and the partner does not send an amount.

| Field | Rule |
|---|---|
| `external_reference` | Required |
| `approved_on` | Optional, `YYYY-MM-DD`. Default today (Kigali), which is also what the template proposes. It cannot be in the future. It cannot be after the loan's expected disbursement date, because Fineract refuses that. |

```
POST /api/loans/LN-2026-000003/approve {"approved_on": "2026-10-14", "external_reference": "P25-APR-TPL-FUT"}
-> 422 {"error": {"code": "VALIDATION_ERROR", "details": {"problems": [{"field": "approved_on", "problem": "cannot be in the future (Kigali date)"}]}, "message": "approved_on: cannot be in the future (Kigali date)", ...}}
```

A date after the expected disbursement date gets `422` with the problem `must be on or before the expected disbursement date <date>`. Through the partner API, that case can only happen once a loan's expected date is in the past, so it was not run on the one-day stack. A unit test covers our check. Fineract's own refusal was run straight on Fineract on 2026-10-13: [DECISIONS.md](DECISIONS.md) has the details.

The approval is from D-1 (`make demo-lifecycle`). The `409` is from an earlier run whose output was not kept:

```
POST /api/loans/LN-2026-000001/approve {"external_reference": "APR-B63B2F-2429"}
-> 200 {"approved_on": "2026-10-13", "approved_principal": 5000000, "client_uid": "DEMO-371457", "external_reference": "APR-B63B2F-2429", "loan_reference": "LN-2026-000001", "status": "APPROVED"}

the same loan, a new reference
-> 409 {"error": {"code": "LOAN_ALREADY_APPROVED", "correlation_id": "c-docs-…", "details": {"holder_status": "COMPLETED"}, "message": "This loan is already approved, or being approved, under another external_reference."}}
```

#### `GET /api/loans/{loan_reference}`

The loan as Fineract shows it now. `status.code` is Fineract's status in our form, `status.value` is Fineract's own label. `balances` is `null` until the loan is paid out. `payoff_amount` is Fineract's own figure for paying the loan off today (its `prepayLoan` template). It is only set while the loan is active. Paying exactly that amount closed the D-1 loan.

An answer for an active loan, from an earlier run (output not kept):

```json
{ "loan_reference": "LN-2026-000002", "client_uid": "DEMO-555080",
  "status": {"code": "ACTIVE", "value": "Active"}, "closed_obligations_met": false, "overpaid": false,
  "currency": "RWF", "principal": 1000000, "approved_principal": 1000000,
  "submitted_on": "2026-10-13", "approved_on": "2026-10-13", "expected_disbursement_on": "2026-10-13",
  "disbursed_on": "2026-10-13", "closed_on": null, "expected_maturity_on": "2027-10-13",
  "balances": { "principal_disbursed": 1000000, "principal_repaid": 0, "principal_outstanding": 1000000,
                "interest_charged": 100161, "interest_repaid": 0, "interest_outstanding": 100161,
                "fees_outstanding": 0, "penalties_outstanding": 0, "total_expected_repayment": 1100161,
                "total_repaid": 0, "total_outstanding": 1100161, "total_overdue": 0, "overpaid_amount": 0 },
  "payoff_amount": 1100161 }
```

After the payoff, the D-1 loan showed `"status": {"code": "CLOSED_OBLIGATIONS_MET", "value": "Closed (obligations met)"}`, `closed_on` set, every outstanding balance `0` and `payoff_amount` `null`.

#### `POST /api/loans/{loan_reference}/disburse`

Pays out the loan once, for the amount in Fineract's disbursement template. That is the approved amount. The partner does not send an amount. Fineract's transaction gets `externalId` `DSB:<external_reference>`.

| Field | Rule |
|---|---|
| `external_reference` | Required |
| `transaction_date` | Optional, `YYYY-MM-DD`, not in the future (Kigali). Default **today**, the day the money moves. It is not the loan's expected disbursement date. |

A payout before the expected disbursement date is allowed. A loan applied for with `expected_disbursement_date` 2026-10-20 was paid out on 2026-10-13. A real answer, from a run of the form endpoints on 2026-10-13:

```
POST /api/loans/LN-2026-000004/disburse {"external_reference": "P25-DSB-TPL-0002"}
-> 200 {"amount": 1000000, ..., "loan_status": "ACTIVE", "status": "ERP_PENDING", "transaction_date": "2026-10-13", "type": "DISBURSEMENT"}
```

Real run, from D-1 (`make demo-lifecycle`):

```
POST /api/loans/LN-2026-000001/disburse {"external_reference": "DSB-B63B2F-0d4d"}
-> 200 {"amount": 5000000, "client_uid": "DEMO-371457", "erp_status": "PENDING", "external_reference": "DSB-B63B2F-0d4d", "loan_reference": "LN-2026-000001", "loan_status": "ACTIVE", "status": "ERP_PENDING", "transaction_date": "2026-10-13", "type": "DISBURSEMENT"}
```

`amount` and `transaction_date` come from Fineract's own transaction, read back after the call. `loan_status` is the loan's status right after it.

| Situation | Response |
|---|---|
| The same request 4 times in a row (I-1, `make simulate-duplicates`) | `200` four times, identical bodies. One Fineract disbursement, one ERP entry. |
| Another reference for a loan already paid out | `409 LOAN_ALREADY_DISBURSED`, `details.holder_status` |
| The loan is not approved yet | `422 VALIDATION_ERROR` |
| Fineract refuses because the loan is already paid out outside our service | `409 LOAN_ALREADY_DISBURSED` |
| Unknown loan | `404 LOAN_NOT_FOUND` |

One of the nine refused requests in I-2b, sent while the winner was still running (`make simulate-concurrent-disburse`):

```
POST /api/loans/LN-2026-000014/disburse, 10 at once, each with its own external_reference
-> 409 {"error": {"code": "LOAN_ALREADY_DISBURSED", "correlation_id": "c-demo-9a4f1600-001", "details": {"holder_status": "FINERACT_PENDING"}, "message": "This loan is already paid out, or being paid out, under another external_reference."}}
```

#### `POST /api/loans/{loan_reference}/repayments`

Records one repayment. Fineract decides how it is split between principal and interest. The answer shows Fineract's split as it is.

| Field | Rule |
|---|---|
| `external_reference` | Required. One per real repayment. |
| `amount` | Required. Whole RWF above 0. `1000.5` gets `422`. |
| `transaction_date` | Optional, not in the future (Kigali). Default today. |
| `payment_type` | Optional. The name of a partner payment type from Fineract's repayment template, like `Money Transfer`, in any case. Fineract's system-defined types (its own adjustments, like `Repayment Adjustment Refund`) and unknown names get `422`. |

The amount and date rules and the payment types come from Fineract's repayment template, read under the loan's lock right before the write. `GET /api/loans/{loan_reference}/template?step=repayment` shows the same rules, with the next installment as the default amount.

```
POST /api/loans/LN-2026-000003/repayments {"amount": 100000, "external_reference": "P25-RPY-TPL-SYS", "payment_type": "Repayment Adjustment Refund"}
-> 422 {"error": {"code": "VALIDATION_ERROR", "details": {"problems": [{"field": "payment_type", "problem": "must be one of: Money Transfer"}]}, "message": "payment_type: must be one of: Money Transfer", ...}}
```

Real run, from D-1 (`make demo-lifecycle`):

```
POST /api/loans/LN-2026-000001/repayments {"external_reference": "RPY1-B63B2F-6b79", "amount": 500000}
-> 200 {"amount": 500000, "client_uid": "DEMO-371457", "erp_status": "PENDING", "external_reference": "RPY1-B63B2F-6b79", "fees_portion": 0, "interest_portion": 116600, "loan_reference": "LN-2026-000001", "loan_status": "ACTIVE", "outstanding_after": 4616600, "overpayment_portion": 0, "penalties_portion": 0, "principal_portion": 383400, "status": "ERP_PENDING", "transaction_date": "2026-10-13", "type": "REPAYMENT"}
```

- The portions are Fineract's own split of this transaction.
- `outstanding_after` is the principal still owed, from the loan's summary, read right after the repayment under the loan's lock.
- A repayment is taken only while Fineract shows the loan `ACTIVE`. Fineract itself would take money on a closed loan and book all of it as an overpayment, so we refuse first. From an earlier run (output not kept):

```
POST /api/loans/LN-2026-000001/repayments {"amount": 5000, "external_reference": "DOCS-RPY-CLOSED-0001"}
-> 422 {"error": {"code": "VALIDATION_ERROR", "correlation_id": "c-docs-…", "details": {"loan_status": "CLOSED_OBLIGATIONS_MET"}, "message": "Loan LN-2026-000001 takes repayments only while it is ACTIVE; Fineract shows it CLOSED_OBLIGATIONS_MET."}}
```

- Paying more than is owed on an `ACTIVE` loan is allowed. Fineract books the extra as `overpayment_portion` and the loan becomes `OVERPAID`. The tests check this. It was not part of the D-1 run.

### Forms: what to send at each step

These three reads tell a partner what to send, before it sends it. They read Fineract's own templates live and save nothing. The same templates are read again by the write itself, so the form and the write follow the same rules.

Every form has `fields`. Each field has `name`, `type`, `required`, `read_only`, `default`, `minimum`, `maximum`, `multiple_of`, `pattern`, `max_length`, `options` and `description`. A `read_only` field is shown for information: Fineract or the product sets it, so do not send it. An option's `value` is a name or a code, never a Fineract id. All examples below are real answers from a run of the form endpoints on 2026-10-13, shortened. They have no `make` command. `integration-service/tests/integration/test_templates_api.py` checks the same endpoints against the live Fineract.

#### `GET /api/clients/template`

The fields of `POST /api/clients` with their rules, plus the office and the activation date Fineract uses for a new client.

```json
{ "form": "client", "submit_to": "POST /api/clients", "office": "Kigali Main Office", "activation_date": "2026-10-13",
  "fields": [ {"name": "external_reference", "required": true, "pattern": "^[A-Za-z0-9._:\\-]{1,40}$", "max_length": 40, ...},
              {"name": "client_uid", "required": true, "pattern": "^[A-Z]{2,10}-[0-9]{6}$", ...},
              {"name": "first_name", "required": true, "max_length": 50, ...},
              {"name": "last_name", "required": true, "max_length": 50, ...} ] }
```

#### `GET /api/loans/template`

The fields of `POST /api/loans`, with the product's defaults, range and terms from Fineract's loan template. Add `?client_uid=` for a client created through `POST /api/clients`. An unknown one gets `404 CLIENT_NOT_FOUND`.

```json
{ "form": "loan_application", "submit_to": "POST /api/loans", "client_uid": "TPL-000001",
  "product": { "name": "SME Asset Finance", "short_name": "SMEA",
               "description": "SME asset finance: 18% a year, declining balance, 12 monthly equal installments, paid out once, RWF.",
               "currency": {"code": "RWF", "name": "Rwanda Franc", "decimal_places": 0, "in_multiples_of": 1} },
  "fields": [ {"name": "client_uid", "required": true, "default": "TPL-000001", ...},
              {"name": "principal", "type": "integer", "required": true, "default": 5000000, "minimum": 100000, "maximum": 50000000, "multiple_of": 1, ...},
              {"name": "expected_disbursement_date", "type": "date", "required": false, "minimum": "2026-10-13", ...},
              {"name": "number_of_repayments", "read_only": true, "default": 12, "minimum": 12, "maximum": 12, ...},
              {"name": "interest_rate_per_period", "read_only": true, "default": 18.0, ...},
              {"name": "interest_type", "read_only": true, "default": "DECLINING_BALANCE", "options": [{"value": "DECLINING_BALANCE", ...}], ...},
              ... ] }
```

The other read-only fields are `repayment_every`, `repayment_frequency`, `loan_term`, `loan_term_frequency`, `interest_rate_frequency`, `amortization` and `interest_calculation_period`.

#### `GET /api/loans/{loan_reference}/template?step=`

The form for one step of one loan: `approval`, `disbursement`, `repayment` or `payoff` (a payoff is sent as a repayment). It has the fields of that call, Fineract's defaults for this loan now, and `values`: what Fineract reports for the step.

Fineract answers a template whatever the loan's status. So we check the status ourselves: `available` is `true` only when the loan's status allows the step now, and `message` says why not. Before approval, only the approval step is available:

```json
{ "form": "disbursement", "loan_reference": "LN-2026-000003", "loan_status": "SUBMITTED_AND_PENDING_APPROVAL",
  "available": false,
  "message": "The disbursement step needs the loan to be APPROVED; Fineract shows it SUBMITTED_AND_PENDING_APPROVAL.",
  "submit_to": "POST /api/loans/LN-2026-000003/disburse",
  "values": {"amount": 2000000, "expected_disbursement_date": "2026-10-13"}, "fields": [...] }
```

| Step | Needs status | `values` | Main fields |
|---|---|---|---|
| `approval` | `SUBMITTED_AND_PENDING_APPROVAL` | `approved_principal`, `expected_disbursement_date` | `approved_on` (default today, at most today), `approved_principal` (read only) |
| `disbursement` | `APPROVED` | `amount` (the approved amount), `expected_disbursement_date` | `transaction_date` (default today, at most today), `amount` (read only) |
| `repayment` | `ACTIVE` | the next installment: `next_due_date`, `next_due_amount` and its split | `amount` (default the next installment), `transaction_date`, `payment_type` (options: the partner payment types) |
| `payoff` | `ACTIVE` | `payoff_amount`, `payoff_date` and its split, from Fineract's `prepayLoan` template | `amount` (default the payoff amount), `transaction_date`, `payment_type` |

A payoff form for an active loan, then paying exactly `payoff_amount` closed that loan (`CLOSED_OBLIGATIONS_MET`):

```json
{ "form": "payoff", "loan_reference": "LN-2026-000003", "loan_status": "ACTIVE", "available": true, "message": null,
  "submit_to": "POST /api/loans/LN-2026-000003/repayments",
  "values": {"payoff_amount": 2000321, "payoff_date": "2026-10-13", "principal_portion": 1846640, "interest_portion": 153681,
             "fee_portion": 0, "penalty_portion": 0, "outstanding_principal": 1846640},
  "fields": [ {"name": "amount", "required": true, "default": 2000321, "minimum": 1, ...},
              {"name": "payment_type", "options": [{"label": "Money Transfer", "value": "Money Transfer"}], ...}, ... ] }
```

An unknown loan gets `404 LOAN_NOT_FOUND`. An unknown step gets `422` with the problem `Input should be 'approval', 'disbursement', 'repayment' or 'payoff'`. A Fineract read that fails gets `503`, `504` or `502`, as for any read.

### `GET /api/transactions/{external_reference}`

Every request this partner sent with that reference, with its full history. Another partner gets `404` for the same reference. Real answer, from 2026-10-14. [DECISIONS.md](DECISIONS.md) shows the log lines of the same request:

```json
{ "external_reference": "FIN-2026-000001",
  "operations": [ {
    "type": "DISBURSEMENT", "op_key": "DSB:FIN-2026-000001", "status": "COMPLETED", "erp_status": "SENT",
    "client_uid": "SME-064407", "loan_reference": "LN-2026-000022", "attempts": 1, "last_error": null,
    "created_at": "2026-10-14T10:22:09.732831Z", "updated_at": "2026-10-14T10:22:12.103925Z",
    "history": [
      {"from": null, "to": "RECEIVED", "at": "2026-10-14T10:22:09.734437Z", "detail": "request saved"},
      {"from": "RECEIVED", "to": "FINERACT_PENDING", "at": "2026-10-14T10:22:09.737489Z", "detail": "saved and claimed"},
      {"from": "FINERACT_PENDING", "to": "FINERACT_DONE", "at": "2026-10-14T10:22:10.248513Z", "detail": "Fineract said yes"},
      {"from": "FINERACT_DONE", "to": "ERP_PENDING", "at": "2026-10-14T10:22:10.259925Z", "detail": "journal entry waiting for the ERP"},
      {"from": "ERP_PENDING", "to": "COMPLETED", "at": "2026-10-14T10:22:12.107013Z", "detail": "the ERP saved the journal entry"} ] } ] }
```

A failed send to the ERP shows as a history line from `ERP_PENDING` to `ERP_PENDING`, with the reason. An unknown reference gets `404 NOT_FOUND`.

### `POST /admin/recover`

Runs one recovery pass now: requests stuck in `FINERACT_PENDING` are looked up in Fineract and finished or marked `FAILED`, then due outbox rows are sent. The same pass runs on its own every 5 seconds. Today it takes the same partner key as `/api`. In production it would need its own operator key. The answer has counts only:

```json
{ "operations": {"checked": 0, "completed": 0, "failed": 0, "finished_meanwhile": 0, "needs_reconciliation": 0, "skipped_busy": 0, "still_unknown": 0},
  "outbox": {"claimed": 0, "needs_reconciliation": 0, "rate_limited": 0, "retried": 0, "sent": 0, "stale": 0} }
```

### `POST /api/reconciliation`

Compares Fineract's loan transactions with the ERP's journal entries. It is read only. It writes nothing to Fineract, the ERP or our own tables, and the report is not stored. Each call reads the sources again.

**Request.** Send one of these two shapes, never both:

| Field | Rule |
|---|---|
| `from`, `to` | Dates, `YYYY-MM-DD`. Both are needed. `to` is included and must not be before `from`. At most 31 days. The date is the Fineract transaction date and the ERP entry date. |
| `references` | 1 to 100 values. Each is an op key (`DSB:FIN-2026-000001`), a partner reference (`FIN-2026-000001`, which selects both `DSB:` and `RPY:`) or a foreign Fineract `externalId`. Each one also selects its `:REV` reversal. |
| Anything else | `422` |

```
{"from": "2026-10-01", "to": "2026-10-31"}
{"references": ["FIN-2026-000001", "RPY:FIN-2026-000007"]}
```

**What is compared.** Every `DISBURSEMENT` and `REPAYMENT` of every loan in our records, matched to the ERP entry whose `external_reference` equals the transaction's `externalId`. Other Fineract types, such as accruals, are only counted in `not_compared`.

**Outcomes:**

| `outcome` | Meaning |
|---|---|
| `MATCHED` | Both sides exist. The ERP lines equal the lines our mapping builds from Fineract's own split, with the same source and date. |
| `MISSING_IN_ERP` | In Fineract, not in the ERP, and not waiting in our outbox |
| `MISSING_IN_FINERACT` | In the ERP, but no transaction of a loan we know has that `externalId` |
| `AMOUNT_MISMATCH` | Both sides exist but differ: an account, a side, an amount, the source or the date. Each difference is one line in `reasons`. |
| `IN_FLIGHT` | Not in the ERP yet, but our operation has done its Fineract part and its outbox row is not sent yet. The outbox will send it, so it is not counted as missing. |

**Answer fields:**

| Field | Meaning |
|---|---|
| `scope` | The request, as received (`from`, `to`, `references`) |
| `generated_at` | When the report was made (UTC) |
| `compared_types` | `["DISBURSEMENT", "REPAYMENT"]` |
| `summary` | A count per outcome, and `total` |
| `not_compared.fineract_types` | Fineract transactions that were skipped, counted by type, for example `{"ACCRUAL": 16}` |
| `not_compared.erp_entries` | ERP entries whose reference is not an op key |
| `warnings` | Things worth a look that do not stop the report, for example `loan LN-2026-000009 is in our records but not in Fineract` |
| `items` | One per key, described below |

Each item:

| Field | Meaning |
|---|---|
| `key` | The matching key: the Fineract `externalId`, which is also the ERP `external_reference`. `null` for a Fineract transaction with no `externalId`. |
| `outcome` | One of the five above |
| `loan_reference` | Our loan reference, when the Fineract side is known |
| `type` | `DISBURSEMENT`, `REPAYMENT` or `REVERSAL` |
| `fineract` | Fineract's transaction: `type`, `transaction_date`, `amount`, the five portions and `reversed`. `null` when missing. |
| `erp` | The ERP entry: `entry_date`, `source`, `reverses`, `total` and `lines`. `null` when missing. |
| `expected_lines` | The lines our Part H mapping builds from Fineract's own transaction. `null` when there is no Fineract side. |
| `ours` | Our operation `status` and outbox `erp_status`. `null` when the transaction was not made through this service. |
| `reasons` | Why the item is not `MATCHED`, in our own words |

No Fineract numeric id appears in the answer.

**Errors:**

| Situation | Response |
|---|---|
| Both shapes, neither, a missing `from` or `to`, `to` before `from`, more than 31 days, more than 100 references, a bad reference | `422 VALIDATION_ERROR` |
| The ERP has 1000 or more entries on a single day | `422 VALIDATION_ERROR`: send references instead |
| Fineract can't be read | `503 FINERACT_UNAVAILABLE`, `504 FINERACT_TIMEOUT` or `502 FINERACT_UNAVAILABLE` |
| The ERP can't be read | `503 ERP_UNAVAILABLE`, `504 ERP_TIMEOUT` or `502 ERP_UNAVAILABLE` |

It is all or nothing. If one read fails, there is no report. A report built from half the data would show false `MISSING` items.

**A real run** (K-1, `make simulate-recon`, on 2026-10-14). Five items were planted on purpose: a disbursement and a repayment that match, a repayment whose ERP entry has the wrong split, a repayment made straight in Fineract with its own `externalId`, and an ERP entry with no Fineract transaction.

```
POST /api/reconciliation {"references": ["DSB:K1-DSB-FC9343-8aca", "RPY:K1-RPY-FC9343-a98a", "RPY:K1-MIS-FC9343-128c", "MANUAL-FC9343", "RPY:K1-FC9343-GHOST"]}
-> 200
```

```json
{ "scope": {"from": null, "references": ["DSB:K1-DSB-FC9343-8aca", "..."], "to": null},
  "summary": {"AMOUNT_MISMATCH": 1, "IN_FLIGHT": 0, "MATCHED": 2, "MISSING_IN_ERP": 1, "MISSING_IN_FINERACT": 1, "total": 5},
  "not_compared": {"erp_entries": 0, "fineract_types": {"ACCRUAL": 16}},
  "warnings": [],
  "items": [
    { "key": "RPY:K1-MIS-FC9343-128c", "outcome": "AMOUNT_MISMATCH", "type": "REPAYMENT", "loan_reference": "LN-2026-000016",
      "fineract": {"amount": 50000, "principal_portion": 44470, "interest_portion": 5530, "overpayment_portion": 0,
                   "fees_portion": 0, "penalties_portion": 0, "reversed": false, "transaction_date": "2026-10-14", "type": "REPAYMENT"},
      "erp": {"entry_date": "2026-10-14", "source": "REPAYMENT", "reverses": null, "total": 50000,
              "lines": [{"account_code": "1100", "debit": 50000, "credit": 0}, {"account_code": "1200", "debit": 0, "credit": 50000}]},
      "expected_lines": [{"account_code": "1100", "debit": 50000, "credit": 0}, {"account_code": "1200", "debit": 0, "credit": 44470},
                         {"account_code": "4100", "debit": 0, "credit": 5530}],
      "ours": {"status": "NEEDS_RECONCILIATION", "erp_status": "NEEDS_RECONCILIATION"},
      "reasons": ["account 1200 credit: ERP 50000, from Fineract 44470", "account 4100 credit: ERP 0, from Fineract 5530"] },
    { "key": "MANUAL-FC9343", "outcome": "MISSING_IN_ERP", "type": "REPAYMENT", "erp": null, "ours": null,
      "reasons": ["the ERP has no entry MANUAL-FC9343", "externalId MANUAL-FC9343 is not one of our operation keys: made outside the Integration Service"], "...": "..." },
    { "key": "RPY:K1-FC9343-GHOST", "outcome": "MISSING_IN_FINERACT", "type": "REPAYMENT", "fineract": null, "expected_lines": null,
      "reasons": ["no Fineract transaction with externalId RPY:K1-FC9343-GHOST on any loan we know"], "...": "..." },
    { "key": "DSB:K1-DSB-FC9343-8aca", "outcome": "MATCHED", "reasons": [], "...": "..." },
    { "key": "RPY:K1-RPY-FC9343-a98a", "outcome": "MATCHED", "reasons": [], "...": "..." } ] }
```

The items are shortened. The script also prints them in full, and a second report for the whole day. That day report counted `{'AMOUNT_MISMATCH': 2, 'IN_FLIGHT': 0, 'MATCHED': 28, 'MISSING_IN_ERP': 4, 'MISSING_IN_FINERACT': 72, 'total': 106}`, because other runs had used the same stack that day. `generated_at` and `compared_types` are not printed by the script.

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
| 405 | `invalid_request` | A method the path does not take, like `DELETE /api/accounts`. Other HTTP errors keep their own status and also use `invalid_request`, except `404`, which is `not_found`. |
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

The first call gets `201` with this body. A repeat of the same entry gets `200` with the same body, except `"status": "ALREADY_EXISTS"`:

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
