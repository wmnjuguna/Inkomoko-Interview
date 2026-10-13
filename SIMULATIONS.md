# Simulations

> **Status:** none have been run yet. Each result below will be copied from a real run, never written before it.

## What each result records

1. What things looked like before
2. The request
3. The response
4. What Fineract shows
5. What the ERP shows
6. What the Integration Service shows
7. What should happen
8. What did happen
9. Why

## The list

| ID | Part | What it tests | Command | Status |
|---|---|---|---|---|
| A-1 | A | The plugin's four rules: new, same data, different data, no permission | `make demo-gl-sync` | Not run |
| A-2 | A | The same ERP account sent 4 times gives 1 ERP account, 1 GL account and 1 mapping | `make simulate-gl-sync-4x` | Not run |
| C-1 | C | Create a client, find it by `client_uid`, check it | `make demo-identity` | Not run |
| D-1 | D | A loan from application to Closed: approve, disburse, 3 repayments, pay off | `make demo-lifecycle` | Not run |
| I-1 | I | The same disbursement sent 4 times, one after another | `make simulate-duplicates` | Not run |
| I-2a | I | 10 disbursements at the same time, same reference | `make simulate-concurrent-disburse` | Not run |
| I-2b | I | 10 disbursements at the same time, different references | `make simulate-concurrent-disburse` | Not run |
| I-3 | I | Repayments at the same time, different references, plus duplicates | `make simulate-concurrent-repay` | Not run |
| J-1 | J | The ERP is down after Fineract succeeds | `make simulate-j1` | Not run |
| J-2 | J | Fineract is down | `make simulate-j2` | Not run |
| J-3 | J | Fineract times out after the request was sent | `make simulate-j3` | Not run |
| J-4 | J | The ERP times out after it saved the entry | `make simulate-j4` | Not run |
| J-5 | J | A crash between Fineract succeeding and the ERP call | `make simulate-j5` | Not run |
| K-1 | K | Reconciliation, with all four outcomes set up on purpose | `make simulate-recon` | Not run |

## What we expect (to be checked by the runs)

**I-1**

```
Requests received:        4
Fineract disbursements:   1
ERP journal entries:      1
Integration transactions: 1
```

**I-3:** Fineract's final unpaid principal equals the starting principal minus the principal of each repayment, as Fineract recorded it. A repeated reference is only counted once.

**K-1, how each outcome is set up:**

- MATCHED: a normal loan payment.
- MISSING_IN_ERP: a payment made while the ERP is down and the outbox is paused.
- MISSING_IN_FINERACT: a journal entry posted straight to the ERP.
- AMOUNT_MISMATCH: an ERP entry posted with a different amount.

## Results

*(Added after each run, using the list above.)*

### Mock ERP fault checks (direct, before the Integration Service exists)

These are **not** the J-1 and J-4 runs in the table. Those go through the Integration Service, which is not built yet. Here I called the Mock ERP API directly, to check that its fault switch behaves the way J-1 and J-4 will need. Run on the Mock ERP container on 2026-10-13.

| Check | What I did | What happened |
|---|---|---|
| Like J-1: ERP down | Set the fault to `down`, posted a journal entry, then posted it again | First call `503`. The retry got `201`. |
| Like J-4: ERP slow after saving | Set `timeout_after_write` with a 2 s wait. The client gave up after 1 s. Then I sent the same entry again. | The retry got `200` (`ALREADY_EXISTS`). Only one entry was saved. |
| Same reference, different amounts | Posted the J-4 reference again with other amounts | `409 reference_conflict`. Nothing changed. |
| Rate limited | Set `rate_limited` with `retry_after` 7, then listed the accounts | `429` with the header `Retry-After: 7` |

The ERP wrote one JSON log line per request, with the correlation id and the fault. For example, the call hit by `timeout_after_write` (shortened):

```json
{"method": "POST", "path": "/api/journal-entries", "status": 201, "duration_ms": 2017.4,
 "fault": "timeout_after_write", "external_reference": "E2E-0002",
 "correlation_id": "c-4a5431ecb75e49489e8234023ded5fde"}
```

The server still answered `201` after 2 s, but the client had already left. Its retry got `200` with the same `erp_transaction_id` (`6bd5ec9b-…`).
