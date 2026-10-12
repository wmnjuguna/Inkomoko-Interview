# API

> **Status:** contract draft. Examples will be replaced with captured responses once the services run. OpenAPI is served by each FastAPI service at `/docs` and `/openapi.json` (planned).

## Conventions (Integration Service)

- **Auth:** `X-API-Key: <key>` per partner, stored hashed. Missing or wrong key → `401`.
- **Correlation:** `X-Correlation-ID` is accepted or generated, echoed on every response, written to every log line, and forwarded to Fineract and the ERP.
- **Identifiers:** callers use `client_uid` (`SME-000001`), `loan_reference` (`LN-2026-000001`, issued by us) and `external_reference` (one per business operation). Fineract numeric IDs are never accepted or returned.
- **Money:** RWF, integer amounts (RWF has no minor unit).

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

## Fineract custom endpoint *(planned)*

`PUT /fineract-provider/api/v1/glaccounts/sync/{externalReference}`. Basic auth as the least-privilege API user, tenant header `Fineract-Platform-TenantId: default`, requires permission `SYNC_GLACCOUNT`.

| Case | Response |
|---|---|
| New reference | 200, new `resourceId` |
| Same reference, identical payload | 200, existing `resourceId` |
| Same reference, different payload | 409 |
| GL code already used by another account | 409 |
| Missing permission | 403 |
