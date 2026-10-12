# API

> **Status:** contract draft. Examples will be replaced with captured responses once the services run.

## OpenAPI specifications *(planned)*

Each API has a machine-readable OpenAPI 3 spec generated from its code. `make openapi` exports them to `docs/openapi/`, lints them with Redocly, and fails if an export drifts from the running service.

| API | Served at | Exported to |
|---|---|---|
| Integration Service | `/openapi.json`, `/docs` | `docs/openapi/integration-service.yaml` |
| Mock ERP | `/openapi.json`, `/docs` | `docs/openapi/mock-erp.yaml` |
| Fineract ERP accounts (custom module) | `/fineract-provider/glsync.json` (and `.yaml`) | `docs/openapi/fineract-erp-accounts.yaml` |

Every operation documents its summary, operation ID, all response codes (including 202, 409, 422 and 425) with the error envelope and examples, the `X-API-Key` security scheme, and the `X-Correlation-ID` header. The Fineract endpoint needs its own spec because Fineract's built-in `fineract.json` is generated at build time from `fineract-provider` only, and doesn't include custom modules.

## Conventions (Integration Service)

- **Auth:** `X-API-Key: <key>` per partner, stored hashed. Missing or wrong key → `401`.
- **Correlation:** `X-Correlation-ID` is accepted or generated, echoed on every response, written to every log line, and forwarded to Fineract and the ERP.
- **Identifiers:** callers use `client_uid` (`SME-000001`), `loan_reference` (`LN-2026-000001`, issued by us) and `external_reference` (one per business operation). Fineract numeric IDs are never accepted or returned.
- **Money:** RWF, integer amounts (RWF has no minor unit).

### Rules for `external_reference` (partner contract)

- Required on every state-changing request; missing → `422`.
- Generate it **once**, when the business action is decided (e.g. "disburse loan X"), store it, and resend the **same value on every retry**. Never derive it from a timestamp or create a new one per HTTP attempt.
- One reference per business action. Two genuine repayments of the same amount on the same day need two references.
- At most 40 characters of `[A-Za-z0-9._:-]`; longer → `422`. This keeps the derived Fineract key within Fineract's 50-character limit.
- `X-Correlation-ID` is different: it traces a request and may change on every retry.

### Error envelope

```json
{
  "error": {
    "code": "IDEMPOTENCY_CONFLICT",
    "message": "external_reference DSB-0001 was already used with a different payload",
    "correlation_id": "6f1c…",
    "details": {}
  }
}
```

| HTTP | `code` examples |
|---|---|
| 400 / 422 | `VALIDATION_ERROR` |
| 401 | `UNAUTHENTICATED` |
| 404 | `NOT_FOUND` |
| 409 | `IDEMPOTENCY_CONFLICT`, `CLIENT_UID_TAKEN`, `LOAN_ALREADY_DISBURSED`, `GL_CODE_TAKEN` |
| 502 / 503 / 504 | `FINERACT_UNAVAILABLE`, `FINERACT_TIMEOUT` (outcome being resolved) |

## Idempotency contract

Every state-changing request carries `external_reference`. The request hash is SHA-256 over canonical JSON of the business payload.

| Case | Response |
|---|---|
| New reference | Processed once. `201` (or `200`) with the result |
| Same reference, same payload, finished | `200` with the **stored original body**, byte for byte |
| Same reference, same payload, in progress | `202` with current status; never a second execution |
| Same reference, different payload | `409 IDEMPOTENCY_CONFLICT` |

**"Finished"** means the Fineract leg is done and its response is stored. A disbursement or repayment whose ERP posting is still pending is finished; its live status comes from `GET /api/transactions/{external_reference}`. A replay of a stale in-progress operation also triggers the lookup-based recovery for that operation.

| Operation | Uniqueness scope |
|---|---|
| GL sync | per `external_reference`; ERP `code` also unique |
| Client creation | per `external_reference`; `client_uid` unique at DB level (reuse with a new reference → 409) |
| Loan application | per `external_reference` |
| Approval | per `external_reference`; once per loan |
| Disbursement | per `external_reference`; once per loan, even across different references |
| Repayment | per `external_reference`; many per loan |

## Integration Service endpoints *(planned)*

| Method & path | Purpose | Success | Notes |
|---|---|---|---|
| `POST /api/accounts/sync` | Pull ERP chart of accounts and sync to Fineract GL | 200 | Re-runnable; per-account `sync_status` |
| `POST /api/clients` | Create client | 201 | Body: `external_reference, client_uid, name, …` |
| `GET /api/clients/{client_uid}` | Find and validate client | 200 | Returns exists / active / name |
| `POST /api/loans` | Loan application | 201 | Returns issued `loan_reference` |
| `POST /api/loans/{loan_reference}/approve` | Approval | 200 | Once per loan |
| `GET /api/loans/{loan_reference}` | Loan status and balances (as Fineract reports them) | 200 | |
| `POST /api/loans/{loan_reference}/disburse` | Disbursement | 200 | `erp_status` in body |
| `POST /api/loans/{loan_reference}/repayments` | Repayment | 200 | Principal / interest split from Fineract |
| `GET /api/transactions/{external_reference}` | Operation status and timeline | 200 | The "what happened to X?" call |
| `POST /api/reconciliation` | Fineract vs ERP report, by date range or references | 200 | Read-only |
| `POST /admin/faults` | Configure fault injection | 200 | Not for partners |
| `POST /admin/recover` | Force a recovery pass | 200 | Not for partners |

## Mock ERP endpoints *(planned)*

| Method & path | Purpose |
|---|---|
| `POST /api/accounts` | Create chart-of-accounts entry; idempotent on `external_reference` |
| `GET /api/accounts` | List accounts |
| `GET /api/accounts/{code}/balance` | Account balance |
| `POST /api/journal-entries` | Post a balanced entry; idempotent on `external_reference`; unbalanced → 422 |
| `GET /api/journal-entries/{external_reference}` | Fetch an entry |
| `POST /admin/faults` | `down`, `timeout`, `timeout-after-write`, `429` |

## Fineract custom endpoints: ERP accounts

Served by the `glsync` custom module inside Fineract. Full contract, with every field constraint and error example: [`docs/openapi/fineract-erp-accounts.yaml`](docs/openapi/fineract-erp-accounts.yaml), also served at `/fineract-provider/glsync.json`.

Basic auth, tenant header `Fineract-Platform-TenantId: default`. Only the Integration Service calls these, as part of `POST /api/accounts/sync`; GL accounts are never created in Fineract any other way.

### `PUT /fineract-provider/api/v1/erp-accounts/{erpReference}`

Permission `SYNC_GLACCOUNT`. Body: core's GL account fields.

| Field | Rule (as enforced by Fineract 1.15.0) |
|---|---|
| `name` | required, 1–200 characters |
| `glCode` | required, 1–45 characters, unique across GL accounts |
| `type` | required: `1` ASSET, `2` LIABILITY, `3` EQUITY, `4` INCOME, `5` EXPENSE |
| `usage` | required: `1` DETAIL, `2` HEADER (core's validator allows null but the column is NOT NULL, so the module requires it) |
| `manualEntriesAllowed` | required boolean |
| `description` | optional, ≤ 500 characters |
| `parentId`, `tagId` | optional; parent must exist and be a HEADER account; tag must be a valid code value |
| anything else | `400 error.msg.parameter.unsupported` |
| `{erpReference}` | 1–100 characters of `A–Z a–z 0–9 . _ : -` |

| Case | Response |
|---|---|
| New reference, GL code free | `200 {"erpReference":"GL-1200","glAccountId":1,"glCode":"1200","outcome":"CREATED"}` |
| Same reference, same payload (key order, whitespace and nulls ignored) | `200 … "outcome":"UNCHANGED"`, nothing written |
| Same reference, different payload | `409 error.msg.glaccount.sync.payload.conflict`, nothing written |
| New reference, GL code already used | `409 error.msg.glaccount.sync.code.conflict`, nothing written |
| Missing permission | `403 error.msg.not.authorized` |
| Repeated `Idempotency-Key` | original body replayed, header `x-served-from-cache: true` |
| Same `Idempotency-Key` while the first call runs | `425` |

### `GET /fineract-provider/api/v1/erp-accounts/{erpReference}`

Permission `READ_GLACCOUNT`. `200 {"erpReference","glAccountId","glCode","name","syncedAt"}`, or `404` if not synced yet. Used to resolve a sync whose response was lost, instead of retrying blindly.
