# Decisions

One decision per line: what I chose and why. The diagrams are in [ARCHITECTURE.md](ARCHITECTURE.md), the endpoints in [API.md](API.md), and the runs behind each claim in [SIMULATIONS.md](SIMULATIONS.md).

1. **Fineract is extended with a plugin, not patched.** Fineract takes custom modules in its `custom/` folder. Mine keeps the ERP's reference on each GL account, which Fineract 1.15 has no field for, and refuses a second account for the same reference. Patching Fineract's own code would make every upgrade a merge, so upstream stays a pinned 1.15.0 submodule.

2. **The plugin runs inside Fineract's own transaction.** Its one write, `PUT /v1/erp-integration/accounts/{erpReference}`, takes the same path as Fineract's own commands, so Fineract checks the permission and writes its audit record, and the GL account, my reference row and that record commit together or not at all. A database lock on the reference makes ten parallel requests give one account.

3. **Flask for both Python services.** One stack means one way to validate, log, migrate and test. Pydantic checks every request, bad input gets `422` in one shape, and the OpenAPI spec is generated from the same models.

4. **Docker only.** `cp .env.example .env` and `docker compose up -d --build --wait` build everything and start it from an empty database. Nothing else to install for the stack itself.

5. **One key names each operation.** `DSB:FIN-2026-000001` is my row key, the `externalId` I give Fineract and the `external_reference` I give the ERP, so reconciliation matches all three systems on one value.

6. **Idempotency lives in Postgres.** Each request is one row per type and reference, with a hash of the caller and body and, once done, the saved answer. The same request again gets that answer as `200`, or `202` while the first is still running; a different body under the same reference gets `409`. Fineract's own `Idempotency-Key` only replays answers, so the guard on its side is the unique `externalId`, and I send both.

7. **The database refuses a second payout.** A rule allows one live client creation per `client_uid` and one live approval and one live payout per loan. A second reference for a payout fails at the insert, so nothing is saved or sent and the caller gets `409 LOAN_ALREADY_DISBURSED`. Writes to one loan also wait on a lock; without it, ten parallel repayments lost five to Fineract's own `409`.

8. **Synchronous to Fineract, asynchronous to the ERP.** The caller waits for Fineract, because that is where the money moves. The ERP entry goes into an outbox in the same transaction and is sent within five seconds, so an ERP outage blocks nobody.

9. **No Redis.** Fineract keeps its cache in its own memory, and my shared state, the idempotency rows, the per-loan lock and the outbox, has to be durable and commit with the operation. Postgres does that; Redis cannot.

10. **No Kafka.** Fineract's events are off by default and go out after the write, at least once, so they cannot stop a duplicate, and every consumer would have to deduplicate again. I enforce idempotency at the write, over REST, and keep the asynchronous pattern for the ERP through the outbox, without a broker.

11. **Never retry blind.** A timeout, a `5xx` or a `425` triggers a lookup by `externalId` before anything is resent. Not found means Fineract may still be working, so the request stays `FINERACT_PENDING`: `504` now, `202` on repeats, and a second look by recovery after 180 seconds, which alone may mark it `FAILED`. The outbox backs off up to 60 seconds and gives up after eight tries at `NEEDS_RECONCILIATION`, where a person looks.

12. **Failures are injected on purpose.** A small fault proxy in front of Fineract can cut it off or drop the answer to one request, which toxiproxy cannot do because it works on TCP. A crash hook, on only when `FAULT_CRASH_AFTER_FINERACT_DONE` names a reference prefix, kills the process right after the commit so recovery can be watched finishing the job.

13. **Fineract's templates are the rulebook.** Before each write I read Fineract's template for that step and take the rules from it: the principal range, the approved amount, the payment types, the figure that clears the loan. One extra read per write, and no copy of Fineract's rules to drift.

14. **Journal entries use Fineract's own split.** After Fineract says yes I read the transaction back and post its principal, interest and overpayment portions, never my own maths. The loan product is cash-based, so every Fineract posting maps to exactly one ERP entry under the same reference.

15. **Reconciliation is a live, read-only report** keyed on the operation key. `IN_FLIGHT` covers entries still in my outbox, and `AMOUNT_MISMATCH` compares the split, not just the total. If either side cannot be read, the call fails rather than show false gaps.

16. **Left out for time:** reversals, a repair call for `NEEDS_RECONCILIATION`, the other Part L items (fees, metrics, scheduled reconciliation, signed webhooks), and Prometheus, Grafana and OpenTelemetry, which the brief lists as optional. The JSON logs carry every field the brief asks for, and one `grep` on a correlation id tells a request's story across both services.

## What happened to FIN-2026-000001?

One call answers it:

```
GET /api/transactions/FIN-2026-000001
-> 200 DISBURSEMENT, op_key DSB:FIN-2026-000001, loan LN-2026-000022, status COMPLETED, erp_status SENT, attempts 1
   history: RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED
```

The correlation id finds the same request in both services' logs:

```
$ docker compose logs --no-log-prefix integration-service | grep c-fin-2026-000001
{"timestamp": "2026-10-14T10:22:10.265811+00:00", "level": "INFO", "message": "request", "correlation_id": "c-fin-2026-000001", "method": "POST", "path": "/api/loans/LN-2026-000022/disburse", "status": "ERP_PENDING", "http_status": 200, "duration_ms": 549.5, "partner": "demo-partner", "external_reference": "FIN-2026-000001", "loan_reference": "LN-2026-000022", "client_uid": "SME-064407"}
{"timestamp": "2026-10-14T10:22:12.107445+00:00", "level": "INFO", "message": "operation status", "correlation_id": "c-fin-2026-000001", "op_key": "DSB:FIN-2026-000001", "external_reference": "FIN-2026-000001", "client_uid": "SME-064407", "loan_reference": "LN-2026-000022", "fineract_transaction_id": 319, "erp_transaction_id": "646fbfe7-dedd-482f-bcca-6430d72ad731", "status": "COMPLETED", "previous_status": "ERP_PENDING", "detail": "the ERP saved the journal entry"}

$ docker compose logs --no-log-prefix mock-erp | grep c-fin-2026-000001
{"timestamp": "2026-10-14T10:22:12.098378+00:00", "level": "INFO", "message": "request", "correlation_id": "c-fin-2026-000001", "method": "POST", "path": "/api/journal-entries", "status": 201, "duration_ms": 8.8, "external_reference": "DSB:FIN-2026-000001", "erp_transaction_id": "646fbfe7-dedd-482f-bcca-6430d72ad731"}
```

Fineract said yes with transaction `319`, the caller had its `200` in 549 ms, and the ERP saved the entry two seconds later.

## Assumptions

- A module in Fineract's `custom/` folder counts as extending Fineract.
- A payout may answer `200` while its ERP entry is still pending.
- Each client system has its own API key, and `FIN-2026-000001` is that system's reference.
- For ten payouts at once, "nothing saved or sent" matters more than the word `FAILED`.
- The Kigali office sits under Head Office, because Fineract shows users only their own office and below.

## Known weaknesses

- Reconciliation reads every loan I know. Fine for hundreds, not thousands.
- GL sync only creates. A renamed ERP account is warned about, not applied.
- One kind of API key. Operator routes and other clients' loans would need keys of their own.
