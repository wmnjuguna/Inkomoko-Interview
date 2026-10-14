# Simulations

Every scenario below has a `make` command. Each one calls the Integration Service like a partner, then reads Fineract, the ERP and the databases to show the state before and after. Each run uses new ids, so it can run again. Run them after `make up`.

Each result below is copied from a real run. The full output of each run is in `evidence/<id>.txt`. [evidence/README.md](evidence/README.md) lists every file.

## What each result records

This is the brief's evidence template. Every script prints these nine points.

1. Initial state
2. Request
3. Response
4. Fineract state
5. ERP state
6. Integration state
7. Expected result
8. Actual result
9. Explanation

## The list

| ID | Part | What it tests | Command | Result (from the run) | Evidence |
|---|---|---|---|---|---|
| A-1 | A | The plugin's four rules: one mapping, same data, different data, no permission | `make demo-gl-sync` | `4 of 4 behaviours matched`, twice | [A-1.txt](evidence/A-1.txt) |
| A-2 | A | A new ERP account sent 4 times at once | `make simulate-gl-sync-4x` | `round 1: PASS, round 2: PASS` | [A-2.txt](evidence/A-2.txt) |
| C-1 | C | Create a client, find it by `client_uid`, check it | `make demo-identity` | `5 of 5 steps matched` | [C-1.txt](evidence/C-1.txt) |
| D-1 | D | A loan from application to Closed | `make demo-lifecycle` | `8 of 8 checks matched` | [D-1.txt](evidence/D-1.txt) |
| I-1 | I | The same disbursement sent 4 times, one after another | `make simulate-duplicates` | Requests 4, Fineract 1, ERP 1, operations 1. `PASS` | [I-1.txt](evidence/I-1.txt) |
| I-2a | I | 10 disbursements at once, same reference | `make simulate-concurrent-disburse` | `Result I-2a: PASS` | [I-2.txt](evidence/I-2.txt) |
| I-2b | I | 10 disbursements at once, different references | `make simulate-concurrent-disburse` | `Result I-2b: PASS` | [I-2.txt](evidence/I-2.txt) |
| I-3 | I | 10 repayments at once: 6 references plus copies | `make simulate-concurrent-repay` | Fineract repayments 6, operations 6, outbox 6, ERP 6. `PASS` | [I-3.txt](evidence/I-3.txt) |
| J-1 | J | The ERP is down after Fineract succeeds | `make simulate-j1` | `PASS` | [J-1.txt](evidence/J-1.txt) |
| J-2 | J | Fineract is down | `make simulate-j2` | `PASS` | [J-2.txt](evidence/J-2.txt) |
| J-3 | J | Fineract times out after the request was sent | `make simulate-j3` | `PASS` | [J-3.txt](evidence/J-3.txt) |
| J-4 | J | The ERP times out after it saved the entry | `make simulate-j4` | `PASS` | [J-4.txt](evidence/J-4.txt) |
| J-5 | J | A crash between Fineract succeeding and the ERP call | `make simulate-j5` | `PASS` | [J-5.txt](evidence/J-5.txt) |
| K-1 | K | Reconciliation, with all four outcomes planted | `make simulate-recon` | 2 `MATCHED`, 1 of each other outcome. `PASS` | [K-1.txt](evidence/K-1.txt) |

A-1, A-2, C-1, D-1 and I-1 were run on 2026-10-13, each on a clean stack. I-2 to K-1 were run on 2026-10-14, one after another, on the running stack, with every fault off at the start and at the end.

## Results

### A-1: the plugin's four rules

Run on 2026-10-13, on a clean stack (`docker compose down -v`, then `make up`). Setup had synced the ERP chart. Full output of both runs: [evidence/A-1.txt](evidence/A-1.txt).

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

Run on 2026-10-13, on the same clean stack, right after A-1. Full output: [evidence/A-2.txt](evidence/A-2.txt).

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

Run on 2026-10-13, on a clean stack. Command: `make demo-identity`. Full output: [evidence/C-1.txt](evidence/C-1.txt). Run `54E587`, `client_uid` `DEMO-797009`. It printed `5 of 5 steps matched` and exited 0.

| Step | Request | What happened |
|---|---|---|
| 1. Create | `POST /api/clients`, Test Client One | `201`, status `ACTIVE`. Fineract has 1 client with that `externalId`, active, in Kigali Main Office. 1 operation, `RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> COMPLETED`. No ERP entry, as expected. |
| 2. Find and check | `GET /api/clients/DEMO-797009?name=Test Client One`, then with `Someone Else` | `200`, active, `name_matches` true, then false. Nothing saved. |
| 3. Same request again | the body of step 1 | `200` with the same body. Still 1 client in Fineract. |
| 4. Same reference, other data | last name `Client Two` | `409 IDEMPOTENCY_CONFLICT`. The name in Fineract is still Test Client One. |
| 5. New reference, same `client_uid` | another reference | `409 CLIENT_UID_TAKEN`. Still 1 client, and the new reference was not saved. |

**Why:** the client's `externalId` in Fineract is the `client_uid`. The service saves each request under `CLI:<reference>` with a hash, and `client_uid` is unique in our database and in Fineract.

### D-1: a loan from application to Closed

Run on 2026-10-13, on a clean stack (`docker compose down -v`, then `docker compose up -d --no-build --wait`). Command: `make demo-lifecycle`. Full output: [evidence/D-1.txt](evidence/D-1.txt). Run `B63B2F`, client `DEMO-371457`, loan `LN-2026-000001`. It printed `8 of 8 checks matched` and exited 0.

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
| `DSB:DSB-B63B2F-0d4d` | DR 1200 5,000,000 / CR 1100 5,000,000 |
| `RPY:RPY1-B63B2F-6b79` | DR 1100 500,000 / CR 1200 383,400 / CR 4100 116,600 |
| `RPY:RPY2-B63B2F-f2ff` | DR 1100 750,000 / CR 1200 658,939 / CR 4100 91,061 |
| `RPY:RPY3-B63B2F-cef0` | DR 1100 1,000,000 / CR 1200 891,040 / CR 4100 108,960 |
| `RPY:RPYOFF-B63B2F-257f` | DR 1100 3,250,799 / CR 1200 3,066,621 / CR 4100 184,178 |

- After every stage, the ERP totals per account equalled Fineract's own GL entries for the loan. At the end: 1100 DR 5,500,799 CR 5,000,000; 1200 DR 5,000,000 CR 5,000,000; 4100 CR 500,799.
- The payout and the 4 repayments each ended `COMPLETED` with `erp_status` `SENT`. The application and the approval ended `COMPLETED` with no ERP part.

**Why:** the service reads Fineract's transaction back by its `externalId` and builds the entry only from that split. The entry is saved in the outbox in the same database transaction that marks the request done. `payoff_amount` is Fineract's own figure, so paying it closes the loan.

### I-1: the same disbursement 4 times

Run on 2026-10-13, on the same clean stack as D-1, right after it. Command: `make simulate-duplicates`. Full output: [evidence/I-1.txt](evidence/I-1.txt). Run `37ED48`, loan `LN-2026-000002` (1,000,000 RWF, approved), reference `I1-DSB-37ED48-b597`.

```
Requests received:        4
Fineract disbursements:   1
ERP journal entries:      1
Integration transactions: 1
```

- All 4 answers were `200` with identical bodies.
- Fineract ran 1 command with the key `DSB:I1-DSB-37ED48-b597:<attempt>`.
- The one ERP entry is DR 1200 1,000,000 / CR 1100 1,000,000.
- "Requests received" is counted from the service's request log lines for the script's 4 correlation ids.

Result: `PASS`, exit 0.

**Why:** the first call saves `DSB:<reference>` and pays out once. Calls 2 to 4 find the finished request with the same hash and get its saved answer, so Fineract is not called again. The outbox has one row, so the ERP gets one entry.

### I-2a: 10 disbursements at once, same reference

Run on 2026-10-14. Command: `make simulate-concurrent-disburse`. Full output: [evidence/I-2.txt](evidence/I-2.txt). Run `9A4F16`, loan `LN-2026-000013` (1,000,000 RWF, approved), reference `I2A-DSB-9A4F16-930d`.

1. **Before:** Fineract `200 Approved`, 0 disbursements. No ERP entry. No operation.
2. **Request:** `POST /api/loans/LN-2026-000013/disburse`, the same body 10 times, released together by one barrier.
3. **Response:** 1 x `200`, 9 x `202`. The `200` bodies are identical.
4. **Fineract:** `300 Active`, 1 disbursement. 1 command ran with the key `DSB:I2A-DSB-9A4F16-930d:<attempt>`.
5. **ERP:** 1 entry, `DSB:I2A-DSB-9A4F16-930d`: DR 1200 1,000,000 / CR 1100 1,000,000.
6. **Integration Service:** 1 operation, `COMPLETED`, `erp_status` `SENT`, 1 attempt. 1 outbox row, `SENT`.
7. **Expected:** Fineract 1, commands 1, operations 1, outbox 1, ERP 1.
8. **Actual:** `Requests sent: 10; Fineract disbursements: 1; Fineract commands for DSB:I2A-DSB-9A4F16-930d: 1; Integration operations: 1; outbox rows: 1; ERP journal entries: 1`. `Result I-2a: PASS`.
9. **Why:** the first insert of (DSB, reference) wins on the unique key. The other 9 find that row and get `202` while it runs. Only the winner calls Fineract, and it writes one outbox row in the same transaction as `FINERACT_DONE`.

### I-2b: 10 disbursements at once, different references

Same run, loan `LN-2026-000014`, 10 different references.

1. **Before:** Fineract `200 Approved`, 0 disbursements. No ERP entry. No operation.
2. **Request:** `POST /api/loans/LN-2026-000014/disburse`, 10 at once, each with its own `external_reference`.
3. **Response:** 1 x `200` (winner `I2B-DSB-9A4F16-7775`), 9 x `409 LOAN_ALREADY_DISBURSED` with `details.holder_status` `FINERACT_PENDING`.
4. **Fineract:** `300 Active`, 1 disbursement. 1 command for the winner, 0 for the 9 others.
5. **ERP:** 1 entry, `DSB:I2B-DSB-9A4F16-7775`: DR 1200 1,000,000 / CR 1100 1,000,000.
6. **Integration Service:** 1 operation, `COMPLETED`. Operations rows for the 9 refused references: 0. 1 outbox row, `SENT`.
7. **Expected:** one `200`, nine `409`. Fineract 1. No command and no row for any refused reference. Outbox 1, ERP 1.
8. **Actual:** `Requests sent: 10; Fineract disbursements: 1; Fineract commands for the winner: 1, for the 9 others: 0; Integration operations: 1 (rows for the 9 others: 0); outbox rows: 1; ERP journal entries: 1`. `Result I-2b: PASS`.
9. **Why:** each reference is new, so the same-reference rule does not stop them. The partial unique index `operations_one_live_disbursement` allows one live disbursement per loan. The other 9 inserts fail, so nothing is saved for them and Fineract is never called. The brief says these end `FAILED`. Here they get no row at all. [DECISIONS.md](DECISIONS.md) explains why.

### I-3: 10 repayments at once

Run on 2026-10-14. Command: `make simulate-concurrent-repay`. Full output: [evidence/I-3.txt](evidence/I-3.txt). Run `58D3DE`, loan `LN-2026-000015` (1,000,000 RWF, paid out).

1. **Before:** Fineract `300 Active`, principal outstanding 1,000,000, total 1,100,161. 0 repayments. No ERP repayment entry.
2. **Request:** 10 repayments at once, by one barrier: 6 references of 15,000, 25,000, 35,000, 45,000, 55,000 and 65,000 RWF. The first two were sent 3 times each.
3. **Response:** 6 x `200`, 4 x `202`. The copies got `202` while their first request was running.
4. **Fineract:** 6 repayments, one per reference. Principal outstanding 801,532, total outstanding 860,161. Each split is Fineract's own, for example 15,000 = principal 13,360 + interest 1,640.
5. **ERP:** 6 entries, one per reference, each with Fineract's split, for example `RPY:I3-RPY-58D3DE-4220`: DR 1100 15,000 / CR 1200 13,360 / CR 4100 1,640.
6. **Integration Service:** 6 operations, all `COMPLETED`. 6 outbox rows, all `SENT`.
7. **Expected:** one Fineract repayment, one operation, one outbox row and one ERP entry per reference. The balance drops by exactly what Fineract applied.
8. **Actual:** `Fineract repayments: 6; Integration operations: 6; outbox rows: 6; ERP journal entries: 6; principal outstanding 1,000,000 -> 801,532 (Fineract applied 198,468); total outstanding 1,100,161 -> 860,161 (Fineract applied 240,000)`. Every check `True`. `Result: PASS`.
9. **Why:** each reference is its own operation, and copies get the saved answer. The per-loan lock sends the different references to Fineract one at a time, so none fails on Fineract's own concurrent-update error. The split is read back from Fineract, never computed by us.

### J-1: the ERP is down after Fineract succeeds

Run on 2026-10-14. Command: `make simulate-j1`. Full output: [evidence/J-1.txt](evidence/J-1.txt). Run `E10DAF`, loan `LN-2026-000017`, reference `J1-DSB-E10DAF-17b6`.

1. **Before:** Fineract `200 Approved`, 0 disbursements. No ERP entry. No operation.
2. **Request:** the Mock ERP fault `down` on `/api/journal-entries`. Then `POST /api/loans/LN-2026-000017/disburse`. After the outbox had retried, the fault was switched off. Then the same request again.
3. **Response:** `200`, `status` `ERP_PENDING`, `erp_status` `PENDING`, at once. The repeat got `200` too.
4. **Fineract:** 1 disbursement and 1 command, during and after.
5. **ERP:** 0 entries while down, 1 after.
6. **Integration Service:** while down: `ERP_PENDING`, outbox `PENDING`, 2 failed sends (`ERP not reachable`). After: `COMPLETED`, outbox `SENT` after 3 attempts.
7. **Expected:** the partner is not blocked. The outbox retries with backoff. After the fault is off: 1 disbursement, 1 ERP entry, `COMPLETED`.
8. **Actual:** `answer 200; outbox retried while down: True; completed: True; Fineract disbursements 1; ERP entries 1`. `Result: PASS`.
9. **Why:** the outbox row is saved in the same transaction as `FINERACT_DONE`, so the outage cannot lose it. Each failed send waits longer (1, 2, 4 ... s). The ERP refuses a second entry with the same reference, and a row is `SENT` once, so there is no double posting.

### J-2: Fineract is down

Run on 2026-10-14. Command: `make simulate-j2`. Full output: [evidence/J-2.txt](evidence/J-2.txt). Run `7986CA`, loan `LN-2026-000018`, reference `J2-DSB-7986CA-957e`.

1. **Before:** Fineract `200 Approved`, 0 disbursements. No ERP entry. No operation.
2. **Request:** the fault proxy `down`. Then `POST /api/loans/LN-2026-000018/disburse`. Then the fault off and the same request again.
3. **Response:** while down: `503 FINERACT_UNAVAILABLE`, "Could not read Fineract's disbursement template. Nothing was sent. Send the same request again." After: `200`, `ERP_PENDING`.
4. **Fineract:** while down: still `Approved`, 0 commands. After: `Active`, 1 disbursement, 1 command.
5. **ERP:** 0 entries while down, 1 after.
6. **Integration Service:** while down: operation `FAILED`, no outbox row. After: `COMPLETED`, 2 attempts, history `... FINERACT_PENDING -> FAILED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED`.
7. **Expected:** while down: `503`, `FAILED`, nothing in Fineract or the ERP. A retry with the same reference works once.
8. **Actual:** `while down: 503 FINERACT_UNAVAILABLE, checks held: True; retry: 200, completed True; Fineract disbursements 1; ERP entries 1`. `Result: PASS`.
9. **Why:** a refused connection proves nothing reached Fineract, so the request is `FAILED` and safe to retry. Only the operation and its history are saved, for the record. The retry first looks up the earlier attempt by `externalId`, finds nothing, and sends once.

### J-3: Fineract times out after the request was sent

Run on 2026-10-14. Command: `make simulate-j3`. It takes about 4 minutes. Full output: [evidence/J-3.txt](evidence/J-3.txt). Run `6881A6`, loan `LN-2026-000019`, reference `J3-DSB-6881A6-ed85`.

1. **Before:** Fineract `200 Approved`, 0 disbursements. No ERP entry. No operation.
2. **Request:** the fault proxy `timeout_after_send` for this loan's disburse call: the write reaches Fineract, the answer is thrown away, then the proxy goes down. `POST /api/loans/LN-2026-000019/disburse`. Then the fault off, the same request again, a wait for recovery, and the same request again.
3. **Response:** first: `504 FINERACT_TIMEOUT` after 30.3 s, `details.status` `FINERACT_PENDING`. A repeat while pending: `202`. After recovery: `200`, `ERP_PENDING`.
4. **Fineract:** 1 disbursement and 1 command, already during the timeout. The proxy logged `Fineract answered 200` for the write it dropped.
5. **ERP:** 0 entries during, 1 after.
6. **Integration Service:** during: `FINERACT_PENDING`, no outbox row. Recovery's background pass found the transaction by `externalId`. After: `COMPLETED`, 1 attempt, outbox `SENT`.
7. **Expected:** `504`, then `202` for a repeat. Recovery finds it by `externalId` and finishes it. Still 1 disbursement and 1 Fineract command: no second write.
8. **Actual:** `first 504 FINERACT_TIMEOUT, repeat 202, checks held: True; completed True; Fineract disbursements 1, commands 1; ERP entries 1; replay 200`. `Result: PASS`.
9. **Why:** a timeout means the result is unknown, so nothing is sent again blindly. The lookup right after also failed, so the request stays `FINERACT_PENDING`. After about 180 s, recovery looks the transaction up by our `externalId` (`DSB:<reference>`), finds Fineract's own transaction, and finishes it the normal way. It never posts to Fineract.

### J-4: the ERP times out after it saved the entry

Run on 2026-10-14. Command: `make simulate-j4`. Full output: [evidence/J-4.txt](evidence/J-4.txt). Run `9A06A3`, loan `LN-2026-000020`, reference `J4-DSB-9A06A3-f3d2`.

1. **Before:** Fineract `200 Approved`, 0 disbursements. No ERP entry. No operation.
2. **Request:** the Mock ERP fault `timeout_after_write` (hold 12 s, next journal call only). Then `POST /api/loans/LN-2026-000020/disburse`.
3. **Response:** `200`, `ERP_PENDING`.
4. **Fineract:** 1 disbursement, 1 command.
5. **ERP:** 1 entry already after the first send timed out, and still 1 at the end.
6. **Integration Service:** one send with no answer from the ERP, then a second send. Outbox `SENT` after 2 attempts. Operation `COMPLETED`.
7. **Expected:** the first send times out although the ERP saved the entry. The outbox sends it again and the ERP answers `200` (already there). Exactly 1 ERP entry.
8. **Actual:** `first send timed out: True; ERP held the entry during: True; completed True; outbox attempts 2; ERP entries 1; Fineract disbursements 1`. `Result: PASS`.
9. **Why:** a timeout from the ERP leaves the result unknown, so the row stays `PENDING` and is sent again after a short wait. The ERP keys entries by `external_reference`: the same entry again gets `200` with the first entry, never a second one.

### J-5: a crash between Fineract and the ERP call

Run on 2026-10-14. Command: `make simulate-j5`. It restarts the Integration Service container. Full output: [evidence/J-5.txt](evidence/J-5.txt). Run `DB3195`, loan `LN-2026-000021`, reference `J5-DB3195-DSB`.

1. **Before:** Fineract `200 Approved`, 0 disbursements. No ERP entry. No operation. The container running, 0 restarts, with the crash hook on for `J5-DB3195-`.
2. **Request:** `POST /api/loans/LN-2026-000021/disburse`. After the restart, the same request again.
3. **Response:** first: no answer (`RemoteProtocolError`: the service died mid-request). The retry after the restart: `200`, equal to the saved answer.
4. **Fineract:** 1 disbursement and 1 command, before and after the crash.
5. **ERP:** 0 entries at the crash, 1 after.
6. **Integration Service:** at the crash: `ERP_PENDING`, outbox `PENDING` with 0 attempts. The container restarted once. After: `COMPLETED`, outbox `SENT` after 1 attempt.
7. **Expected:** no answer to the first call. Compose restarts the service, and the start-up recovery sends the row once. The retry gets the saved answer.
8. **Actual:** `at the crash: ERP_PENDING, ERP entries 0; back up True, completed True; container running, exit code 0, restarts 1; retry 200; Fineract disbursements 1; ERP entries 1`. `Result: PASS`.
9. **Why:** `FINERACT_DONE`, the saved answer and the outbox row are one database transaction, so a crash cannot lose the journal entry or leave half a record. The first recovery pass after the restart sends the due row. The retry finds the finished operation and replays its answer without calling Fineract.

At the end the script started the service again without the crash flag: `healthy True, FAULT_CRASH_AFTER_FINERACT_DONE=(empty: off)`.

### K-1: reconciliation with all four outcomes

Run on 2026-10-14, against the live service. Command: `make simulate-recon`. Full output: [evidence/K-1.txt](evidence/K-1.txt). Run `FC9343`, loan `LN-2026-000016` (1,000,000 RWF).

1. **Before:** a new client and loan. Planted on purpose:
   - `DSB:K1-DSB-FC9343-8aca` and `RPY:K1-RPY-FC9343-a98a`: a normal payout and repayment, so `MATCHED`.
   - `RPY:K1-MIS-FC9343-128c`: a repayment whose ERP entry has the wrong split (all 50,000 on 1200), so `AMOUNT_MISMATCH`.
   - `MANUAL-FC9343`: a repayment made straight in Fineract with its own `externalId`, so `MISSING_IN_ERP`.
   - `RPY:K1-FC9343-GHOST`: an entry posted straight to the ERP, so `MISSING_IN_FINERACT`.
   - Also a repayment of 20,000 made straight in Fineract with no `externalId`.
2. **Request:** `POST /api/reconciliation` with the 5 references, then with `{"from": "2026-10-14", "to": "2026-10-14"}`.
3. **Response:** `200` and `200`.
4. **Fineract:** the loan has the payout, the three repayments above, the one with no `externalId`, and an accrual.
5. **ERP:** the payout and the matching repayment as Fineract split them. `RPY:K1-MIS-FC9343-128c` as DR 1100 50,000 / CR 1200 50,000. No `MANUAL-FC9343`. `RPY:K1-FC9343-GHOST` as DR 1100 10,000 / CR 1200 10,000.
6. **Integration Service:** the payout and the matching repayment `COMPLETED`. The mismatched repayment `NEEDS_RECONCILIATION`, because the ERP refused our entry for a reference it already held with other content.
7. **Expected:** by references: exactly the 5 planted items, 2 `MATCHED` and 1 of each other outcome.
8. **Actual:** by references: `{'AMOUNT_MISMATCH': 1, 'IN_FLIGHT': 0, 'MATCHED': 2, 'MISSING_IN_ERP': 1, 'MISSING_IN_FINERACT': 1, 'total': 5}`. The day report held the same 5 items plus the no-`externalId` repayment as `MISSING_IN_ERP`. `Result: PASS`.
9. **Why:** each Fineract payout or repayment of a loan we know is matched to the ERP entry with the same key, its `externalId`. The ERP lines are compared with the lines our mapping builds from Fineract's own split. So the mismatch was caught even though both totals were 50,000. Transactions made straight in Fineract have a foreign key, or none, so the ERP has nothing under it.

The whole-day report also counted 72 `MISSING_IN_FINERACT` and 28 `MATCHED` from other runs on the shared stack. [DECISIONS.md](DECISIONS.md) explains this under "Known weaknesses".
