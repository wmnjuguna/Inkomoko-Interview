# Simulations

> **Status:** A-1 and A-2 (Part A) and C-1, D-1 and I-1 have been run on a clean stack. The others are not run yet. Each result below is copied from a real run, never written before it.

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
| A-1 | A | The plugin's four rules: one mapping, same data, different data, no permission | `make demo-gl-sync` | Run: 4 of 4 matched, twice |
| A-2 | A | A new ERP account sent 4 times at once gives 1 ERP account, 1 GL account and 1 mapping | `make simulate-gl-sync-4x` | Run: PASS |
| C-1 | C | Create a client, find it by `client_uid`, check it | `make demo-identity` | Run: 5 of 5 matched |
| D-1 | D | A loan from application to Closed: approve, disburse, 3 repayments, pay off | `make demo-lifecycle` | Run: 8 of 8 matched, Closed (obligations met) |
| I-1 | I | The same disbursement sent 4 times, one after another | `make simulate-duplicates` | Run: 4 / 1 / 1 / 1, PASS |
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

### A-1: the plugin's four rules

Run on 2026-10-13, on a clean stack (`docker compose down -v`, then `make up`). Setup had synced the ERP chart. Full output of both runs: `evidence/P1/A-1.txt`.

The demo works on `GL-1200` (Loan Receivable). For rule 4 it uses a user `demo-no-sync` whose role only has `READ_GLACCOUNT`. The first run created that user. The second run found it and reused it. Both runs printed `4 of 4 behaviours matched` and exited 0.

| Rule | Request | What happened |
|---|---|---|
| 1. The ERP reference is stored and unique | `GET /erp-integration/accounts/GL-1200` | `200`, glCode 1200, parent GL-1000. Fineract has 1 `glsync_external_ref` row and 1 GL account with code 1200. The table has unique rules on the reference and on the GL account. |
| 2. Same reference, same data | `PUT` with the exact body the service sends | `200 UNCHANGED`, same GL account. The counts stayed at 1. |
| 3. Same reference, different data | `PUT` with the name `Loan Receivable (changed)` | `409 error.msg.glaccount.sync.payload.conflict`. The name in Fineract is still `Loan Receivable`. |
| 4. No `SYNC_GLACCOUNT` permission | `PUT` as `demo-no-sync` | `403 error.msg.not.authorized`. The same user can still read the mapping. |

The Integration Service showed `GL-1200` as `SYNCED`, 1 attempt, during all four.

**Why:** the plugin keeps the reference in its own table with a unique rule. It compares a hash of the data, returns the existing account when it is equal, and refuses different data. Its `PUT` needs `SYNC_GLACCOUNT`.

### A-2: a new ERP account sent 4 times at once

Run on 2026-10-13, on the same clean stack, right after A-1. Full output: `evidence/P1/A-2.txt`.

Each run adds two new accounts to the Mock ERP (EXPENSE, DETAIL, under 5000), with new 8-digit codes starting with 7. So the first create really races. This run used `GL-71114743` and `GL-77402790`.

**Round 1, through the Integration Service:** 4 calls at once to `POST /api/accounts/GL-71114743/sync`.

```
call 1: 200 in_progress False, SYNCED attempts 1
call 2: 202 in_progress True, no row yet
call 3: 202 in_progress True, no row yet
call 4: 202 in_progress True, no row yet
commands Fineract ran for GL-71114743 from the service (should be 1): 1
```

**Round 2, straight to the plugin:** 4 `PUT /erp-integration/accounts/GL-77402790` at once, same body.

```
outcomes: {'UNCHANGED': 3, 'CREATED': 1}; distinct glAccountIds: 1
```

**Counts after both rounds**, the same for each account:

| | Expected | GL-71114743 | GL-77402790 |
|---|---|---|---|
| ERP accounts with this reference | 1 | 1 | 1 |
| Fineract GL accounts with this code | 1 | 1 | 1 |
| `glsync_external_ref` rows | 1 | 1 | 1 |
| `gl_sync` rows | 1 | 1 | 1 |
| `gl_sync` status | SYNCED | SYNCED, 1 attempt | SYNCED, 1 attempt |

Result: `PASS`, exit 0.

**Why:** in the service, a Postgres advisory lock lets only one sync run at a time. The other 3 calls got `202` and sent nothing. In the plugin, a Postgres lock on the reference and on the GL code makes the requests wait in turn. The first creates the account, the others find it and answer `UNCHANGED`.

### C-1: a client, found and checked

Run on 2026-10-13, on a clean stack (`docker compose down -v`, new images for the Mock ERP and the Integration Service, then `docker compose up -d --no-build --wait`; `evidence/P2/setup.txt`). Command: `make demo-identity`. Full output: `evidence/P2/C-1.txt`. Run `9BB508`, `client_uid` `DEMO-238601`. It printed `5 of 5 steps matched` and exited 0.

| Step | Request | What happened |
|---|---|---|
| 1. Create | `POST /api/clients`, Test Client One | `201`, status `ACTIVE`. Fineract has 1 client with that `externalId`, active, in Kigali Main Office. 1 operation, `RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> COMPLETED`. No ERP entry, as expected. |
| 2. Find and check | `GET /api/clients/DEMO-238601?name=Test Client One`, then with `Someone Else` | `200`, active, `name_matches` true, then false. Nothing saved. |
| 3. Same request again | the body of step 1 | `200` with the same body. Still 1 client in Fineract. |
| 4. Same reference, other data | last name `Client Two` | `409 IDEMPOTENCY_CONFLICT`. The name in Fineract is still Test Client One. |
| 5. New reference, same `client_uid` | another reference | `409 CLIENT_UID_TAKEN`. Still 1 client, and the new reference was not saved. |

**Why:** the client's `externalId` in Fineract is the `client_uid`. The service saves each request under `CLI:<reference>` with a hash, and `client_uid` is unique in our database and in Fineract.

### D-1: a loan from application to Closed

Run on 2026-10-13, on the same clean stack, right after C-1. Command: `make demo-lifecycle`. Full output: `evidence/P2/D-1.txt`. Run `BE75C7`, client `DEMO-273604`, loan `LN-2026-000001`. It printed `8 of 8 checks matched` and exited 0.

Fineract's status and balances after each stage, as Fineract reported them:

| Stage | Request | Fineract status | Principal outstanding | Interest outstanding | Total outstanding |
|---|---|---|---|---|---|
| Application | `POST /api/loans`, 5,000,000 | 100 Submitted and pending approval | no summary yet | | |
| Approval | `POST .../approve` | 200 Approved | no summary yet | | |
| Payout | `POST .../disburse` | 300 Active | 5,000,000 | 500,799 | 5,500,799 |
| Repayment 1 | 500,000 | 300 Active | 4,616,600 | 384,199 | 5,000,799 |
| Repayment 2 | 750,000 | 300 Active | 3,957,661 | 293,138 | 4,250,799 |
| Repayment 3 | 1,000,000 | 300 Active | 3,066,621 | 184,178 | 3,250,799 |
| Payoff | 3,250,799 (the loan's `payoff_amount`) | 600 Closed (obligations met) | 0 | 0 | 0 |

The ERP got one entry per money movement, built from Fineract's own split:

| ERP entry | Lines |
|---|---|
| `DSB:DSB-BE75C7-74d4` | DR 1200 5,000,000 / CR 1100 5,000,000 |
| `RPY:RPY1-BE75C7-3d94` | DR 1100 500,000 / CR 1200 383,400 / CR 4100 116,600 |
| `RPY:RPY2-BE75C7-398e` | DR 1100 750,000 / CR 1200 658,939 / CR 4100 91,061 |
| `RPY:RPY3-BE75C7-3b3f` | DR 1100 1,000,000 / CR 1200 891,040 / CR 4100 108,960 |
| `RPY:RPYOFF-BE75C7-bb64` | DR 1100 3,250,799 / CR 1200 3,066,621 / CR 4100 184,178 |

- After every stage, the ERP totals per account equalled Fineract's own GL entries for the loan. At the end: 1100 DR 5,500,799 CR 5,000,000; 1200 DR 5,000,000 CR 5,000,000; 4100 CR 500,799.
- The payout and the 4 repayments each ended `COMPLETED` with `erp_status` `SENT`. The application and the approval ended `COMPLETED` with no ERP part.
- A read-only check after the run (appendix of `D-1.txt`) found each entry balanced, its lines and date equal to the Fineract transaction with the same `externalId`, and every account synced from the ERP with the same code in Fineract.

**Why:** the service reads Fineract's transaction back by its `externalId` and builds the entry only from that split. The entry is saved in the outbox in the same database transaction that marks the request done. `payoff_amount` is Fineract's own figure, so paying it closes the loan.

### I-1: the same disbursement 4 times

Run on 2026-10-13, on the same clean stack, right after D-1. Command: `make simulate-duplicates`. Full output: `evidence/P2/I-1.txt`. Run `B41E91`, loan `LN-2026-000002` (1,000,000 RWF, approved), reference `I1-DSB-B41E91-f798`.

```
Requests received:        4
Fineract disbursements:   1
ERP journal entries:      1
Integration transactions: 1
```

- All 4 answers were `200` with identical bodies.
- Fineract ran 1 command with the key `DSB:I1-DSB-B41E91-f798:<attempt>`.
- The one ERP entry is DR 1200 1,000,000 / CR 1100 1,000,000, and the same check as D-1 passed.
- "Requests received" is counted from the service's request log lines for the script's 4 correlation ids.

Result: `PASS`, exit 0.

**Why:** the first call saves `DSB:<reference>` and pays out once. Calls 2 to 4 find the finished request with the same hash and get its saved answer, so Fineract is not called again. The outbox has one row, so the ERP gets one entry.

### C-1, D-1 and I-1 again, with the template-driven loan flow (P2.5)

After the loan steps moved to Fineract's templates (the principal range, the approved and payout amounts, the payment types and the payoff now come from Fineract), the three runs were repeated on a new clean stack: `docker compose down -v`, new images for the Mock ERP and the Integration Service, then `docker compose up -d --no-build --wait`. Setup exited 0 and health was `UP` (`evidence/P2.5/setup.txt`). All three scripts ran unchanged, apart from the "Why" text in D-1, which now names the templates.

| Run | Command | Result | Full output |
|---|---|---|---|
| C-1 | `make demo-identity` | `5 of 5 steps matched`, exit 0. Run `54E587`, `client_uid` `DEMO-797009`. | `evidence/P2.5/C-1.txt` |
| D-1 | `make demo-lifecycle` | `8 of 8 checks matched`, exit 0. Run `D093AC`, loan `LN-2026-000001`. The appendix check passed. | `evidence/P2.5/D-1.txt` |
| I-1 | `make simulate-duplicates` | Requests 4, Fineract disbursements 1, ERP entries 1, operations 1. `PASS`, exit 0. The appendix check passed. | `evidence/P2.5/I-1.txt` |

- D-1 gave the same figures as the first run: the approval was 5,000,000 (the approval template's amount), the payout was 5,000,000 dated today (the disbursement template's amount), Fineract's status and balances after each stage were the same as in the table above, and the payoff of 3,250,799 closed the loan.
- The same ERP entries were made, with new references, and the ERP totals equalled Fineract's GL after every stage.
- `evidence/P2.5/templates.txt` walks one more loan through the form endpoints and the new `422` cases. `evidence/P2.5/id-leak.txt` shows no Fineract id in any answer, form included.

### Mock ERP fault checks (direct, before the Integration Service existed)

These are **not** the J-1 and J-4 runs in the table. Those go through the Integration Service and are not run yet. Here I called the Mock ERP API directly, to check that its fault switch behaves the way J-1 and J-4 will need. Run on the Mock ERP container on 2026-10-13.

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
