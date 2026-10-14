# Evidence

Saved output of real runs. Nothing here was written by hand. Each file is what the command printed. All data is fake.

The `make` commands need the running stack (`make up`). [SIMULATIONS.md](../SIMULATIONS.md) explains each simulation.

## Simulations

| File | What it proves | Command |
|---|---|---|
| [A-1.txt](A-1.txt) | The plugin's four rules: one stored ERP reference, same data gives the same account, different data gives `409`, no permission gives `403`. Two runs. | `make demo-gl-sync` |
| [A-2.txt](A-2.txt) | A new ERP account sent 4 times at once makes 1 ERP account, 1 GL account and 1 mapping, through the service and straight to the plugin | `make simulate-gl-sync-4x` |
| [C-1.txt](C-1.txt) | A client is created once, found by `client_uid`, replayed, and refused with other data or a taken `client_uid` | `make demo-identity` |
| [D-1.txt](D-1.txt) | A loan from application to Closed. Fineract, ERP and service state after each step. ERP totals equal Fineract's GL. | `make demo-lifecycle` |
| [I-1.txt](I-1.txt) | The same disbursement 4 times: Fineract 1, ERP 1, operations 1 | `make simulate-duplicates` |
| [I-2.txt](I-2.txt) | I-2a: 10 disbursements at once with one reference pay out once. I-2b: 10 references at once give one `200` and nine `409`, with no row saved for the nine. | `make simulate-concurrent-disburse` |
| [I-3.txt](I-3.txt) | 10 repayments at once (6 references plus copies): each applies once, the balance drops by Fineract's own split, the ERP lines equal that split | `make simulate-concurrent-repay` |
| [J-1.txt](J-1.txt) | The ERP is down after Fineract succeeds: the partner gets `200`, the outbox retries, the ERP gets 1 entry | `make simulate-j1` |
| [J-2.txt](J-2.txt) | Fineract is down: `503`, `FAILED`, nothing sent. A retry with the same reference works once. | `make simulate-j2` |
| [J-3.txt](J-3.txt) | Fineract times out after the write landed: `504`, then `202`. Recovery finds it by `externalId` and finishes it, with no second write. | `make simulate-j3` |
| [J-4.txt](J-4.txt) | The ERP times out after it saved the entry: the outbox resends, the ERP answers `200`, still 1 entry | `make simulate-j4` |
| [J-5.txt](J-5.txt) | The service crashes after `FINERACT_DONE`: compose restarts it, recovery sends the entry once, the retry gets the saved answer | `make simulate-j5` |
| [K-1.txt](K-1.txt) | Reconciliation finds all four planted outcomes: 2 `MATCHED`, 1 `AMOUNT_MISMATCH`, 1 `MISSING_IN_ERP`, 1 `MISSING_IN_FINERACT` | `make simulate-recon` |

## Setup, API and observability

| File | What it proves | Command |
|---|---|---|
| [setup-first-run.txt](setup-first-run.txt) | On a clean stack, setup creates the office, currency, role, API user, 13 GL accounts and the loan product | `docker compose down -v`, then `make up` (the `setup` service) |
| [setup-second-run.txt](setup-second-run.txt) | A second setup run says `exists` for every step and writes nothing to Fineract | `docker compose run --rm setup` |
| [office-and-product.txt](office-and-product.txt) | The office and the loan product, read back from Fineract, match the config | Fineract reads as the admin user, after setup. No `make` target. |
| [least-privilege.txt](least-privilege.txt) | The API user can do its job and gets `403` for anything else (create GL accounts, users, roles, products) | A local check script that is not in the repo |
| [templates.txt](templates.txt) | The form endpoints, built from Fineract's templates, and the `422` cases they enforce | `curl` calls to the Integration Service, listed in the file |
| [FIN-2026-000001.txt](FIN-2026-000001.txt) | "What happened to FIN-2026-000001?" answered with one API call and the logs | `curl` calls and `docker compose logs`, listed in the file |

## Tests

| File | What it proves | Command |
|---|---|---|
| [tests.txt](tests.txt) | ruff, mypy and pytest pass for the Mock ERP (216) and the Integration Service (873) on a clean stack. This run is older than the reliability tests below. | `make test-python` |
| [tests-reliability.txt](tests-reliability.txt) | The reliability tests pass: concurrency (`test_concurrency_sims`), faults (`test_faults_live`, `test_faults_hook`) and reconciliation (`test_reconciliation_api`, `test_reconciliation_rules`). 52 passed. | `uv run pytest` on those five files, in `integration-service/` (the command is in the file) |

## Java (Part A)

The Java evidence lives next to the Fineract customization:

| File | What it holds |
|---|---|
| [fineract-customization/patch/glsync.diff](../fineract-customization/patch/glsync.diff) | The whole change to Fineract 1.15.0, as a diff. Only new files. |
| [fineract-customization/build.log](../fineract-customization/build.log) | The image build from source, with the 51 plugin unit tests (`make build`) |
| [fineract-customization/integration-test.log](../fineract-customization/integration-test.log) | The 10 plugin integration tests against the running Fineract (`make test-java`) |
| [fineract-customization/BUILD.md](../fineract-customization/BUILD.md) | How the image is built and tested |
