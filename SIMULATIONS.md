# Simulations

> **Status:** none run yet. Each entry is filled from the output of its `make` target, never written ahead of it. Raw output is kept in `evidence/`.

## Evidence template

Every simulation records:

1. Initial state
2. Request
3. Response
4. Fineract state
5. ERP state
6. Integration state
7. Expected result
8. Actual result
9. Explanation

## Index

| ID | Part | Scenario | Command | Status |
|---|---|---|---|---|
| A-1 | A | Fineract GL sync behaviours 1–4 (new, same, different payload, permission) | `make demo-gl-sync` | Not run |
| A-2 | A | Same ERP account sent 4× → 1 ERP account, 1 GL, 1 mapping | `make simulate-gl-sync-4x` | Not run |
| C-1 | C | Create client → find by `client_uid` → validate | `make demo-identity` | Not run |
| D-1 | D | Loan lifecycle: application → approval → disbursement → 3 repayments → payoff → Closed | `make demo-lifecycle` | Not run |
| I-1 | I | Same disbursement 4× sequentially | `make simulate-duplicates` | Not run |
| I-2a | I | 10 concurrent disbursements, same reference | `make simulate-concurrent-disburse` | Not run |
| I-2b | I | 10 concurrent disbursements, different references | `make simulate-concurrent-disburse` | Not run |
| I-3 | I | Concurrent repayments, different references + duplicates | `make simulate-concurrent-repay` | Not run |
| J-1 | J | ERP down after Fineract success | `make simulate-j1` | Not run |
| J-2 | J | Fineract down | `make simulate-j2` | Not run |
| J-3 | J | Fineract timeout after sending | `make simulate-j3` | Not run |
| J-4 | J | ERP timeout after the entry was written | `make simulate-j4` | Not run |
| J-5 | J | Crash between Fineract success and the ERP call | `make simulate-j5` | Not run |
| K-1 | K | Reconciliation with all four outcomes seeded | `make simulate-recon` | Not run |

## Expected outcomes (to be confirmed by the runs)

**I-1**

```
Requests received:        4
Fineract disbursements:   1
ERP journal entries:      1
Integration transactions: 1
```

**I-3:** Fineract's final outstanding principal equals the initial principal minus the sum of `principalPortion` across the repayment transactions Fineract recorded. Duplicates of one reference apply once.

**K-1 seeding plan:** MATCHED from a normal flow; MISSING_IN_ERP from a transaction made while the ERP is down and the outbox is paused; MISSING_IN_FINERACT from a journal entry posted straight to the ERP; AMOUNT_MISMATCH from an ERP entry posted with a changed amount.

## Results

*(Filled in per simulation, using the template above.)*
