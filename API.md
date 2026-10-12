# API

> **Status:** the Fineract plugin API is built and tested. The Integration Service and Mock ERP APIs are *planned*.

## OpenAPI specs

Each API has an OpenAPI 3 spec. The spec is generated from the code, not written by hand.

| API | Served at | Copy in this repo |
|---|---|---|
| Fineract ERP integration (my plugin) | `/fineract-provider/glsync.json` and `.yaml` | `docs/openapi/fineract-erp-integration.yaml` |
| Integration Service *(planned)* | `/openapi.json`, `/docs` | `docs/openapi/integration-service.yaml` |
| Mock ERP *(planned)* | `/openapi.json`, `/docs` | `docs/openapi/mock-erp.yaml` |

For the plugin, the build reads the annotations on the Java classes and writes the spec. Fineract's own spec (`fineract.json`) only covers Fineract's built-in code, so the plugin makes its own.

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

## Integration Service *(planned)*

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
| 502, 503, 504 | `FINERACT_UNAVAILABLE`, `FINERACT_TIMEOUT` (we are still finding out what happened) |

### Endpoints

| Call | What it does |
|---|---|
| `POST /api/accounts/sync` | Reads the ERP chart of accounts and syncs each account to Fineract. Safe to run again. |
| `POST /api/clients` | Creates a client |
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

## Mock ERP *(planned)*

| Call | What it does |
|---|---|
| `POST /api/accounts` | Adds an account to the chart. The same reference twice does nothing new. |
| `GET /api/accounts` | Lists accounts |
| `GET /api/accounts/{code}/balance` | Shows an account balance |
| `POST /api/journal-entries` | Saves a journal entry. Debits must equal credits, or you get `422`. The same reference twice does nothing new. |
| `GET /api/journal-entries/{external_reference}` | Shows one journal entry |
| `POST /admin/faults` | Makes the ERP act down, slow, slow after saving, or return `429` |
