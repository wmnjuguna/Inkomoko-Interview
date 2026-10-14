# Simulations

I wrote these simulations so nobody has to take the design on trust: each one calls the Integration Service the way a client system would, reads Fineract, the Mock ERP and our own database before and after, and prints the brief's nine evidence points. The results come from runs on 2026-10-13 and 2026-10-14 on the local stack and can be reproduced with the listed `make` commands after `make up` (see [README.md](README.md)); a fresh run makes new loan numbers and references, but the counts and the result line should not change. The mechanisms are in [ARCHITECTURE.md](ARCHITECTURE.md), the trade-offs in [DECISIONS.md](DECISIONS.md) and the endpoints in [API.md](API.md).

## Where the failures hit

Every money movement takes one path, and the five J drills each break it at a different point.

```mermaid
flowchart LR
    P[Client system] --> S[Integration Service]
    S -- "1. write" --> F[Fineract]
    S -- "2. one transaction: FINERACT_DONE, saved answer, outbox row" --> O[(Outbox)]
    O -- "3. send" --> E[Mock ERP]
    J2([J-2 Fineract down]) -.-> F
    J3([J-3 Fineract times out after the write]) -.-> F
    J5([J-5 service crashes after step 2]) -.-> S
    J1([J-1 ERP down]) -.-> E
    J4([J-4 ERP times out after it saved]) -.-> E
```

## The runs at a glance

| ID | Proves | Command | Result |
|---|---|---|---|
| A-1 | Plugin: 1 mapping, same data accepted, different data refused, no permission refused | `make demo-gl-sync` | `4 of 4 behaviours matched`, twice |
| A-2 | New ERP account sent 4 times at once: 1 account everywhere | `make simulate-gl-sync-4x` | `round 1: PASS, round 2: PASS` |
| C-1 | Client created once, found by `client_uid`, never duplicated | `make demo-identity` | `5 of 5 steps matched` |
| D-1 | Loan from application to Closed, ERP = Fineract after every stage | `make demo-lifecycle` | `8 of 8 checks matched` |
| I-1 | Same disbursement 4 times in a row: 1 payout | `make simulate-duplicates` | Requests 4, Fineract 1, ERP 1, operations 1. `PASS` |
| I-2a | 10 disbursements at once, same reference: 1 wins, 9 wait | `make simulate-concurrent-disburse` | `Result I-2a: PASS` |
| I-2b | 10 disbursements at once, different references: 1 wins, 9 refused | `make simulate-concurrent-disburse` | `Result I-2b: PASS` |
| I-3 | 10 repayments at once, 6 references plus copies: each applied once | `make simulate-concurrent-repay` | Fineract repayments 6, operations 6, outbox 6, ERP 6. `PASS` |
| J-1 | ERP down after Fineract succeeds: entry waits, sent once | `make simulate-j1` | `PASS` |
| J-2 | Fineract down: clean failure, safe retry | `make simulate-j2` | `PASS` |
| J-3 | Fineract times out after the write: recovery finds it, no second write | `make simulate-j3` | `PASS` |
| J-4 | ERP times out after it saved: resend does not double post | `make simulate-j4` | `PASS` |
| J-5 | Crash between Fineract and the ERP: nothing lost | `make simulate-j5` | `PASS` |
| K-1 | Reconciliation names all four outcomes | `make simulate-recon` | 2 `MATCHED`, 1 of each other outcome. `PASS` |

## A-1: the plugin's four rules

| Point | Recorded |
|---|---|
| 1. Initial state | Clean stack, ERP chart synced. `GL-1200` (Loan Receivable): 1 `glsync_external_ref` row, 1 GL account with code 1200. User `demo-no-sync`, role `READ_GLACCOUNT` only. |
| 2. Request | `GET /erp-integration/accounts/GL-1200`; `PUT` with the service's exact body; `PUT` with name `Loan Receivable (changed)`; `PUT` as `demo-no-sync`. |
| 3. Response | `200`, glCode 1200, parent GL-1000; `200 UNCHANGED`, same GL account; `409 error.msg.glaccount.sync.payload.conflict`; `403 error.msg.not.authorized`. |
| 4. Fineract state | 1 `glsync_external_ref` row, 1 GL account with code 1200, name `Loan Receivable`, after every call. `demo-no-sync` can still read the mapping. |
| 5. ERP state | Not captured. |
| 6. Integration state | `GL-1200` `SYNCED`, 1 attempt, throughout. |
| 7. Expected result | 1 mapping per reference; same data `UNCHANGED`; different data refused; no `SYNC_GLACCOUNT` permission refused. |
| 8. Actual result | `4 of 4 behaviours matched`, exit 0, on both runs. |
| 9. Explanation | Unique rule on the reference; hash of the data; permission check on `PUT`. |

**Why this matters.** Without the unique rule, a retried sync would have created a second GL account with code 1200, and without the hash a changed retry would have quietly renamed it. The unique rule, the hash and the permission check each caught their own case.

## A-2: a new ERP account sent 4 times at once

| Point | Recorded |
|---|---|
| 1. Initial state | Clean stack, right after A-1. Two new Mock ERP accounts (EXPENSE, DETAIL, under 5000) with fresh 8-digit codes: `GL-71114743` and `GL-77402790`. |
| 2. Request | Round 1, through the service: 4 x `POST /api/accounts/GL-71114743/sync` at once. Round 2, straight to the plugin: 4 x `PUT /erp-integration/accounts/GL-77402790` at once, same body. |
| 3. Response | Round 1: call 1 `200 in_progress False, SYNCED attempts 1`; calls 2 to 4 `202 in_progress True, no row yet`. Round 2: `{'UNCHANGED': 3, 'CREATED': 1}`, distinct glAccountIds 1. |
| 4. Fineract state | Per account: 1 GL account, 1 `glsync_external_ref` row. Commands for `GL-71114743`: 1. |
| 5. ERP state | 1 account per reference. |
| 6. Integration state | 1 `gl_sync` row per account, `SYNCED`, 1 attempt. |
| 7. Expected result | Every count 1, both accounts. |
| 8. Actual result | `round 1: PASS, round 2: PASS`. `PASS`, exit 0. |
| 9. Explanation | Advisory lock in the service; lock on the reference and on the GL code in the plugin. |

**Why this matters.** Four copies of a new account arriving together is where "check, then create" fails, because all four checks see nothing. The service's advisory lock answered three callers `202`, and the plugin's lock let one create while three answered `UNCHANGED`.

## C-1: a client, created once and found by `client_uid`

| Point | Recorded |
|---|---|
| 1. Initial state | Clean stack. `client_uid` `DEMO-797009`. |
| 2. Request | 1. `POST /api/clients`, Test Client One. 2. `GET /api/clients/DEMO-797009?name=Test Client One`, then `Someone Else`. 3. Step 1's body again. 4. Same reference, last name `Client Two`. 5. New reference, same `client_uid`. |
| 3. Response | 1. `201`, `ACTIVE`. 2. `200`, active, `name_matches` true, then false. 3. `200`, same body. 4. `409 IDEMPOTENCY_CONFLICT`. 5. `409 CLIENT_UID_TAKEN`. |
| 4. Fineract state | 1 client with that `externalId`, active, Kigali Main Office, named Test Client One, after every step. |
| 5. ERP state | No entry, as expected. |
| 6. Integration state | 1 operation, `RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> COMPLETED`. Step 2 saved nothing. Step 5's reference not saved. |
| 7. Expected result | 1 client; lookup answered from Fineract; repeat replayed; changed data and a second reference refused. |
| 8. Actual result | `5 of 5 steps matched`, exit 0. |
| 9. Explanation | Fineract `externalId` is the `client_uid`; each request saved under `CLI:<reference>` with a hash; `client_uid` unique in our database and in Fineract. |

**Why this matters.** Two Fineract clients for one person would hang every later loan off the wrong record. The hash refused a changed body under the same reference (step 4), and the unique `client_uid` refused a second reference before Fineract was touched (step 5).

## D-1: a loan from application to Closed

| Point | Recorded |
|---|---|
| 1. Initial state | Clean stack. Client `DEMO-371457`, loan `LN-2026-000001`, both created by the script. |
| 2. Request | `POST /api/loans` for 5,000,000; `POST .../approve`; `POST .../disburse`; repayments 500,000, 750,000, 1,000,000; payoff 3,250,799, the loan's own `payoff_amount`. |
| 3. Response | Not captured. |
| 4. Fineract state | Status, then principal / interest / total outstanding:<br>application `100 Submitted and pending approval`, no summary<br>approval `200 Approved`, no summary<br>payout `300 Active`, 5,000,000 / 500,799 / 5,500,799<br>repayment 1 `300 Active`, 4,616,600 / 384,199 / 5,000,799<br>repayment 2 `300 Active`, 3,957,661 / 293,138 / 4,250,799<br>repayment 3 `300 Active`, 3,066,621 / 184,178 / 3,250,799<br>payoff `600 Closed (obligations met)`, 0 / 0 / 0<br>Fineract GL entries at the end: 1100 DR 5,500,799 CR 5,000,000; 1200 DR 5,000,000 CR 5,000,000; 4100 CR 500,799. |
| 5. ERP state | One entry per money movement:<br>`DSB:DSB-B63B2F-0d4d` DR 1200 5,000,000 / CR 1100 5,000,000<br>`RPY:RPY1-B63B2F-6b79` DR 1100 500,000 / CR 1200 383,400 / CR 4100 116,600<br>`RPY:RPY2-B63B2F-f2ff` DR 1100 750,000 / CR 1200 658,939 / CR 4100 91,061<br>`RPY:RPY3-B63B2F-cef0` DR 1100 1,000,000 / CR 1200 891,040 / CR 4100 108,960<br>`RPY:RPYOFF-B63B2F-257f` DR 1100 3,250,799 / CR 1200 3,066,621 / CR 4100 184,178<br>ERP totals per account equal to Fineract's GL entries after every stage. |
| 6. Integration state | Payout and 4 repayments `COMPLETED`, `erp_status` `SENT`. Application and approval `COMPLETED`, no ERP part. |
| 7. Expected result | Statuses in order; ERP equal to Fineract's ledger after every stage; paying `payoff_amount` closes the loan. |
| 8. Actual result | `8 of 8 checks matched`, exit 0. |
| 9. Explanation | The service reads Fineract's transaction back by `externalId` and builds the entry from that split; outbox row saved in the same transaction as `FINERACT_DONE`. |

**Why this matters.** Had I worked out principal and interest myself, the ERP would have drifted from Fineract's ledger. Reading each transaction back by `externalId` and copying Fineract's split kept them equal after every stage, down to the 184,178 of interest in the payoff.

## I-1: the same disbursement sent 4 times

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000002`, 1,000,000 RWF, `200 Approved`, 0 disbursements. No ERP entry. No operation. |
| 2. Request | `POST /api/loans/LN-2026-000002/disburse {"external_reference": "I1-DSB-37ED48-b597"}`, 4 times, one after another. |
| 3. Response | 4 x `200`. Calls 2 to 4 identical to call 1: `status` `ERP_PENDING`, `erp_status` `PENDING`, `amount` 1000000. |
| 4. Fineract state | `300 Active`, principal disbursed 1,000,000, total outstanding 1,100,161. 1 disbursement. 1 command, key `DSB:I1-DSB-37ED48-b597:<attempt>`. |
| 5. ERP state | 1 entry, `DSB:I1-DSB-37ED48-b597`: DR 1200 1,000,000 / CR 1100 1,000,000. |
| 6. Integration state | 1 operation, `COMPLETED`, `erp_status` `SENT`, 1 attempt, `RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED`. 5 `operation_events` rows. |
| 7. Expected result | Requests 4, Fineract disbursements 1, ERP entries 1, integration transactions 1. Every answer `200`, same body. |
| 8. Actual result | `Requests received: 4; Fineract disbursements: 1; ERP journal entries: 1; Integration transactions: 1`. `Result: PASS`, exit 0. Requests counted from the service's request log for the 4 correlation ids. |
| 9. Explanation | Call 1 saves `DSB:<reference>` and pays out once; calls 2 to 4 find the finished request with the same hash and get its saved answer. |

**Why this matters.** A client system retrying on a flaky network would otherwise have paid the loan out four times. The saved answer stopped it: calls 2 to 4 never reached Fineract, and one outbox row meant one ERP entry.

## I-2a: 10 disbursements at once, same reference

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000013`, 1,000,000 RWF, `200 Approved`, 0 disbursements. No ERP entry. No operation. |
| 2. Request | `POST /api/loans/LN-2026-000013/disburse`, same body 10 times, released together by one barrier. Reference `I2A-DSB-9A4F16-930d`. |
| 3. Response | 1 x `200`, 9 x `202`. |
| 4. Fineract state | `300 Active`, 1 disbursement. 1 command, key `DSB:I2A-DSB-9A4F16-930d:<attempt>`. |
| 5. ERP state | 1 entry, `DSB:I2A-DSB-9A4F16-930d`: DR 1200 1,000,000 / CR 1100 1,000,000. |
| 6. Integration state | 1 operation, `COMPLETED`, `erp_status` `SENT`, 1 attempt. 1 outbox row, `SENT`. |
| 7. Expected result | Fineract 1, commands 1, operations 1, outbox 1, ERP 1. |
| 8. Actual result | `Requests sent: 10; Fineract disbursements: 1; Fineract commands for DSB:I2A-DSB-9A4F16-930d: 1; Integration operations: 1; outbox rows: 1; ERP journal entries: 1`. `Result I-2a: PASS`. |
| 9. Explanation | First insert of (DSB, reference) wins on the unique key; the other 9 find that row and get `202` while it runs. |

**Why this matters.** Ten requests in the same instant arrive before any saved answer exists, so replay alone would have let several through. The unique key on (type, reference) let one insert win, and only the winner called Fineract.

## I-2b: 10 disbursements at once, different references

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000014`, 1,000,000 RWF, `200 Approved`, 0 disbursements. No ERP entry. No operation. |
| 2. Request | `POST /api/loans/LN-2026-000014/disburse`, 10 at once, each with its own `external_reference`. |
| 3. Response | 1 x `200` (winner `I2B-DSB-9A4F16-7775`), 9 x `409 LOAN_ALREADY_DISBURSED`, `details.holder_status` `FINERACT_PENDING`. |
| 4. Fineract state | `300 Active`, 1 disbursement. Commands: winner 1, the 9 others 0. |
| 5. ERP state | 1 entry, `DSB:I2B-DSB-9A4F16-7775`: DR 1200 1,000,000 / CR 1100 1,000,000. |
| 6. Integration state | 1 operation, `COMPLETED`. Rows for the 9 refused references: 0. 1 outbox row, `SENT`. |
| 7. Expected result | 1 x `200`, 9 x `409`. Fineract 1. No command and no row for any refused reference. Outbox 1, ERP 1. |
| 8. Actual result | `Requests sent: 10; Fineract disbursements: 1; Fineract commands for the winner: 1, for the 9 others: 0; Integration operations: 1 (rows for the 9 others: 0); outbox rows: 1; ERP journal entries: 1`. `Result I-2b: PASS`. |
| 9. Explanation | Partial unique index `operations_one_live_disbursement`: one live disbursement per loan, so the other 9 inserts fail before anything is saved. |

**Why this matters.** Every reference is new, so idempotency has nothing to say and each request looks valid. The partial unique index allows one live disbursement per loan; the brief expects the nine to end `FAILED`, and [DECISIONS.md](DECISIONS.md) explains why they get no row instead.

## I-3: 10 repayments at once

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000015`, 1,000,000 RWF, paid out. `300 Active`, principal outstanding 1,000,000, total 1,100,161. 0 repayments. No ERP repayment entry. |
| 2. Request | 10 repayments released by one barrier: 6 references of 15,000, 25,000, 35,000, 45,000, 55,000 and 65,000 RWF, the first two sent 3 times each. |
| 3. Response | 6 x `200`, 4 x `202`. |
| 4. Fineract state | 6 repayments, one per reference. Principal outstanding 801,532, total outstanding 860,161. Fineract's own split, for example 15,000 = principal 13,360 + interest 1,640. |
| 5. ERP state | 6 entries, one per reference, with Fineract's split, for example `RPY:I3-RPY-58D3DE-4220`: DR 1100 15,000 / CR 1200 13,360 / CR 4100 1,640. |
| 6. Integration state | 6 operations `COMPLETED`. 6 outbox rows `SENT`. |
| 7. Expected result | Per reference: 1 Fineract repayment, 1 operation, 1 outbox row, 1 ERP entry. Balance down by exactly what Fineract applied. |
| 8. Actual result | `Fineract repayments: 6; Integration operations: 6; outbox rows: 6; ERP journal entries: 6; principal outstanding 1,000,000 -> 801,532 (Fineract applied 198,468); total outstanding 1,100,161 -> 860,161 (Fineract applied 240,000)`. Every check `True`. `Result: PASS`. |
| 9. Explanation | One operation per reference; copies get the saved answer; a per-loan lock sends the different references to Fineract one at a time. |

**Why this matters.** Fineract refuses concurrent updates to one loan, so six valid payments at once would have hit its errors. The per-loan lock queued them, the saved answer turned the four copies into `202`, and Fineract's split made the ERP lines total exactly the 240,000 applied.

## J-1: the ERP is down after Fineract succeeds

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000017`, `200 Approved`, 0 disbursements. No ERP entry. No operation. |
| 2. Request | Mock ERP fault `down` on `/api/journal-entries`. `POST /api/loans/LN-2026-000017/disburse`, reference `J1-DSB-E10DAF-17b6`. After the outbox had retried: fault off, same request again. |
| 3. Response | `200`, `status` `ERP_PENDING`, `erp_status` `PENDING`, at once. Repeat `200`. |
| 4. Fineract state | 1 disbursement, 1 command, during and after. |
| 5. ERP state | 0 entries while down, 1 after. |
| 6. Integration state | While down: `ERP_PENDING`, outbox `PENDING`, 2 failed sends (`ERP not reachable`). After: `COMPLETED`, outbox `SENT` after 3 attempts. |
| 7. Expected result | Caller not blocked. Outbox retries with backoff. After the fault is off: 1 disbursement, 1 ERP entry, `COMPLETED`. |
| 8. Actual result | `answer 200; outbox retried while down: True; completed: True; Fineract disbursements 1; ERP entries 1`. `Result: PASS`. |
| 9. Explanation | Outbox row saved in the same transaction as `FINERACT_DONE`; each failed send waits longer (1, 2, 4 ... s); the ERP refuses a second entry with the same reference. |

**Why this matters.** The money had already left Fineract, so refusing the client system would have been wrong. The outbox answered `200` at once, retried with growing backoff until the ERP was back, and the ERP's reference key kept the repeat from posting twice.

## J-2: Fineract is down

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000018`, `200 Approved`, 0 disbursements. No ERP entry. No operation. |
| 2. Request | Fault proxy `down`. `POST /api/loans/LN-2026-000018/disburse`, reference `J2-DSB-7986CA-957e`. Fault off, same request again. |
| 3. Response | While down: `503 FINERACT_UNAVAILABLE`, "Could not read Fineract's disbursement template. Nothing was sent. Send the same request again." After: `200`, `ERP_PENDING`. |
| 4. Fineract state | While down: `Approved`, 0 commands. After: `Active`, 1 disbursement, 1 command. |
| 5. ERP state | 0 entries while down, 1 after. |
| 6. Integration state | While down: `FAILED`, no outbox row. After: `COMPLETED`, 2 attempts, `... FINERACT_PENDING -> FAILED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED`. |
| 7. Expected result | While down: `503`, `FAILED`, nothing in Fineract or the ERP. A retry with the same reference works once. |
| 8. Actual result | `while down: 503 FINERACT_UNAVAILABLE, checks held: True; retry: 200, completed True; Fineract disbursements 1; ERP entries 1`. `Result: PASS`. |
| 9. Explanation | A refused connection proves nothing reached Fineract, so `FAILED` and safe to retry; the retry looks up the earlier attempt by `externalId`, finds nothing, sends once. |

**Why this matters.** The risk here is treating an easy outage as a hard one. A refused connection proves nothing reached Fineract, so the service answered `503` with a `FAILED` operation, and the retry reopened it (`FAILED -> FINERACT_PENDING`) and sent once.

## J-3: Fineract times out after the write

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000019`, `200 Approved`, 0 disbursements. No ERP entry. No operation. |
| 2. Request | Fault proxy `timeout_after_send` on this loan's disburse call: the write reaches Fineract, the answer is dropped, the proxy goes down. `POST /api/loans/LN-2026-000019/disburse`, reference `J3-DSB-6881A6-ed85`. Fault off, same request again, wait for recovery, same request once more. About 4 minutes in all. |
| 3. Response | First: `504 FINERACT_TIMEOUT` after 30.3 s, `details.status` `FINERACT_PENDING`. Repeat while pending: `202`. After recovery: `200`, `ERP_PENDING`. |
| 4. Fineract state | 1 disbursement, 1 command, already during the timeout. The proxy logged `Fineract answered 200` for the dropped write. |
| 5. ERP state | 0 entries during, 1 after. |
| 6. Integration state | During: `FINERACT_PENDING`, no outbox row. Recovery found the transaction by `externalId`. After: `COMPLETED`, 1 attempt, outbox `SENT`. |
| 7. Expected result | `504`, then `202` for a repeat. Recovery finds it by `externalId` and finishes it. 1 disbursement, 1 command: no second write. |
| 8. Actual result | `first 504 FINERACT_TIMEOUT, repeat 202, checks held: True; completed True; Fineract disbursements 1, commands 1; ERP entries 1; replay 200`. `Result: PASS`. |
| 9. Explanation | Timeout = unknown, so no blind resend; after about 180 s recovery finds the transaction by `externalId` (`DSB:<reference>`) and finishes without posting to Fineract. |

**Why this matters.** This is the failure I worry about most: Fineract paid out, we were never told, and a blind retry would have disbursed twice. Treating the timeout as unknown stopped it: the repeat got `202`, and recovery found the transaction by `externalId` without posting again.

## J-4: the ERP times out after it saved the entry

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000020`, `200 Approved`, 0 disbursements. No ERP entry. No operation. |
| 2. Request | Mock ERP fault `timeout_after_write` (hold 12 s, next journal call only). `POST /api/loans/LN-2026-000020/disburse`, reference `J4-DSB-9A06A3-f3d2`. |
| 3. Response | `200`, `ERP_PENDING`. |
| 4. Fineract state | 1 disbursement, 1 command. |
| 5. ERP state | 1 entry already after the first send timed out, still 1 at the end. |
| 6. Integration state | First send: no answer from the ERP. Second send: outbox `SENT`, 2 attempts. Operation `COMPLETED`. |
| 7. Expected result | First send times out although the ERP saved the entry. Outbox resends, ERP answers `200` (already there). ERP entries 1. |
| 8. Actual result | `first send timed out: True; ERP held the entry during: True; completed True; outbox attempts 2; ERP entries 1; Fineract disbursements 1`. `Result: PASS`. |
| 9. Explanation | An ERP timeout leaves the result unknown, so the row stays `PENDING` and is resent after a short wait; the ERP keys entries by `external_reference`, so the resend gets `200` with the first entry. |

**Why this matters.** This is J-3 on the ERP side: the entry was saved but the answer was lost, so an unkeyed resend would have posted twice. The ERP keys entries by reference, so the resend came back `200` with the entry it already held.

## J-5: a crash between Fineract and the ERP call

| Point | Recorded |
|---|---|
| 1. Initial state | Loan `LN-2026-000021`, `200 Approved`, 0 disbursements. No ERP entry. No operation. Container running, 0 restarts, crash hook on for references starting `J5-DB3195-`. |
| 2. Request | `POST /api/loans/LN-2026-000021/disburse`, reference `J5-DB3195-DSB`. After the restart, same request again. |
| 3. Response | First: no answer (`RemoteProtocolError`, the service died mid-request). Retry after the restart: `200`, equal to the saved answer. |
| 4. Fineract state | 1 disbursement, 1 command, before and after the crash. |
| 5. ERP state | 0 entries at the crash, 1 after. |
| 6. Integration state | At the crash: `ERP_PENDING`, outbox `PENDING`, 0 attempts. Container restarted once. After: `COMPLETED`, outbox `SENT` after 1 attempt. Service started again without the crash flag at the end: `healthy True, FAULT_CRASH_AFTER_FINERACT_DONE=(empty: off)`. |
| 7. Expected result | No answer to the first call. Compose restarts the service; start-up recovery sends the row once. Retry gets the saved answer. |
| 8. Actual result | `at the crash: ERP_PENDING, ERP entries 0; back up True, completed True; container running, exit code 0, restarts 1; retry 200; Fineract disbursements 1; ERP entries 1`. `Result: PASS`. |
| 9. Explanation | `FINERACT_DONE`, the saved answer and the outbox row are one database transaction; the first recovery pass after the restart sends the due row; the retry replays the saved answer. |

**Why this matters.** If the Fineract write and the outbox row were separate, this crash would have left a payout with no journal entry. One transaction for `FINERACT_DONE`, the saved answer and the outbox row stopped it: the row was waiting with 0 attempts, recovery sent it, and the retry replayed the saved answer.

## K-1: reconciliation with all four outcomes

| Point | Recorded |
|---|---|
| 1. Initial state | New client, loan `LN-2026-000016`, 1,000,000 RWF, five items planted on purpose. `DSB:K1-DSB-FC9343-8aca` and `RPY:K1-RPY-FC9343-a98a`: normal payout and repayment (`MATCHED`). `RPY:K1-MIS-FC9343-128c`: ERP split wrong, all 50,000 on 1200 (`AMOUNT_MISMATCH`). `MANUAL-FC9343`: repayment made straight in Fineract with its own `externalId` (`MISSING_IN_ERP`). `RPY:K1-FC9343-GHOST`: posted straight to the ERP (`MISSING_IN_FINERACT`). Plus a 20,000 repayment straight in Fineract, no `externalId`. |
| 2. Request | `POST /api/reconciliation` with the 5 references, then with `{"from": "2026-10-14", "to": "2026-10-14"}`. |
| 3. Response | `200` and `200`. |
| 4. Fineract state | The payout, the three repayments above, the no-`externalId` repayment, an accrual. |
| 5. ERP state | Payout and matching repayment with Fineract's split. `RPY:K1-MIS-FC9343-128c`: DR 1100 50,000 / CR 1200 50,000. No `MANUAL-FC9343`. `RPY:K1-FC9343-GHOST`: DR 1100 10,000 / CR 1200 10,000. |
| 6. Integration state | Payout and matching repayment `COMPLETED`. Mismatched repayment `NEEDS_RECONCILIATION`: the ERP refused our entry for a reference it already held with other content. |
| 7. Expected result | By references: exactly the 5 planted items, 2 `MATCHED`, 1 of each other outcome. |
| 8. Actual result | By references: `{'AMOUNT_MISMATCH': 1, 'IN_FLIGHT': 0, 'MATCHED': 2, 'MISSING_IN_ERP': 1, 'MISSING_IN_FINERACT': 1, 'total': 5}`. Day report: the same 5, plus the no-`externalId` repayment as `MISSING_IN_ERP`, plus 72 `MISSING_IN_FINERACT` and 28 `MATCHED` left by other runs on the shared stack. `Result: PASS`. |
| 9. Explanation | Each Fineract payout or repayment of a known loan matched to the ERP entry under its `externalId`; ERP lines compared with lines built from Fineract's split. Direct Fineract transactions carry a foreign key or none, so the ERP has nothing under it. |

**Why this matters.** Both sides said 50,000, so a check on totals alone would have called the mismatch matched. Comparing the ERP lines with the lines built from Fineract's split caught it; the 72 leftovers in the day report come from other runs, a weakness listed in [DECISIONS.md](DECISIONS.md).
