# Fineract and ERP integration

This is my take-home assessment. It connects three things:

- **Apache Fineract**, the core banking system. I changed it with a small Java plugin.
- **A mock ERP**, written in Python. It owns the chart of accounts.
- **An integration service**, written in Python. It sits between partners, Fineract and the ERP.

The goal is to show that money moves exactly once, even with duplicate requests, timeouts, crashes and requests that arrive at the same time.

> **Status:** Tiers 1 and 2 (Parts A to K) are built, tested and run on the local stack. Tier 3 (Part L) is not built. Every simulation in [SIMULATIONS.md](SIMULATIONS.md) has a `make` command and its recorded result.

## What is done

| Part | What | Status | Shown by |
|---|---|---|---|
| A | Fineract plugin: GL accounts synced from the ERP, once | Done, tested, run | `make demo-gl-sync`, `make simulate-gl-sync-4x`, [build.log](fineract-customization/build.log), [integration-test.log](fineract-customization/integration-test.log) |
| B | Fineract setup: office, currency, role, API user, GL accounts, loan product | Done, tested, run twice | The two setup runs below, `integration-service/tests/unit/test_setup_run.py`, `integration-service/tests/integration/test_setup_live.py`, `integration-service/tests/integration/test_least_privilege_live.py` |
| C | Clients | Done, tested, run | `make demo-identity` |
| D | Loan lifecycle, application to Closed | Done, tested, run | `make demo-lifecycle` |
| E | The Integration API, with reconciliation | Done, tested | [API.md](API.md), `integration-service/tests/integration/test_templates_api.py` |
| F | Duplicate requests | Done, tested, run | `make simulate-duplicates` |
| G | Mock ERP, with fault switches | Done, tested | [API.md](API.md) |
| H | Journal entries from Fineract's own split, through an outbox | Done, tested, run | `make demo-lifecycle`, `make simulate-concurrent-repay` |
| I | Concurrency: I-1, I-2a, I-2b, I-3 | Done, tested, run | `make simulate-duplicates`, `make simulate-concurrent-disburse`, `make simulate-concurrent-repay` |
| J | Failures J-1 to J-5, recovery | Done, tested, run | `make simulate-j1` to `make simulate-j5`, the FIN-2026-000001 trail in [DECISIONS.md](DECISIONS.md) |
| K | Reconciliation, all four outcomes | Done, tested, run | `make simulate-recon` |
| L | Stretch | Not built. See "What I chose not to build" in [DECISIONS.md](DECISIONS.md). | |

The results of every run are written down in [SIMULATIONS.md](SIMULATIONS.md). They were captured on 2026-10-13 and 2026-10-14 on the local stack. Each can be reproduced with the `make` command listed next to it. The `make` commands are in the "Simulations" section below.

## Versions

| What | Version | Where it is pinned |
|---|---|---|
| Apache Fineract | `1.15.0`, commit `d5636847ac556c30b437254c353f05526d172b97` | git submodule `fineract-customization/fineract` |
| JDK | 21 | Fineract's own build file |
| Build tool | Gradle 8.14.5 | the Gradle wrapper inside the tag |
| Database | PostgreSQL 18.3 (Alpine image) | `docker-compose.yml` |
| Python | 3.13 (image `python:3.13.16-alpine3.24`) | `pyproject.toml` and `Dockerfile` in `mock-erp/` and `integration-service/` |
| Python packages | Flask 3, flask-openapi3, Pydantic 2, SQLAlchemy 2, Alembic, psycopg 3, Gunicorn, and httpx (Integration Service) | `uv.lock` in each Python service |

## Get the code

```bash
git clone --recurse-submodules <this-repo>
# or, if you already cloned without submodules:
git submodule update --init --depth 1
```

`fineract-customization/fineract` is the original Fineract 1.15.0 source. I never edit it. My Java code lives next to it, in `fineract-customization/custom/`. The build copies it into Fineract.

## Run it

```bash
make build    # builds Fineract from source and runs the Java unit tests, then the Mock ERP, fault proxy and Integration Service images
make up       # starts Postgres, Fineract, the fault proxy, the Mock ERP, runs the setup once, then starts the Integration Service
```

`make up` creates `.env` from `.env.example` the first time. On a clean stack it took about two minutes, most of it waiting for Fineract.

To rebuild only the Python images:

```bash
docker buildx build --load -t mock-erp-inkomoko:0.1.0 mock-erp
docker buildx build --load -t fault-proxy-inkomoko:0.1.0 fault-proxy
docker buildx build --load -t integration-service-inkomoko:0.1.0 integration-service
docker compose up -d --no-build --wait
```

The Mock ERP and the Integration Service update their own database tables when they start. The Mock ERP also adds a small starting chart of accounts.

### The setup step (Part B)

`setup` is a one-shot service in `docker-compose.yml`. It uses the Integration Service image and Fineract's admin user. It does these steps, in order:

1. Waits until Fineract and the ERP are healthy.
2. Creates the office `Kigali Main Office` under Head Office. It finds the office by its external id, `OFF-KGL-MAIN`.
3. Adds RWF to the organisation's currencies.
4. Creates the role `Integration Service` with exactly 11 permissions.
5. Creates the API user `integration-api` in that office with that role, then logs in as it once to check it works.
6. Syncs the whole ERP chart of accounts to Fineract (GL sync, Part A).
7. Stops unless all 9 GL accounts the loan product needs are synced.
8. Creates the loan product `SME Asset Finance`.

The settings are JSON files in `fineract/configuration/`. They hold only our choices, by name or code (`"parent": "Head Office"`, `"repaymentFrequency": "MONTHS"`), never a Fineract id. Setup reads Fineract's templates (`/offices/template`, `/users/template`, `/currencies`, `/permissions`, `/loanproducts/template`) to turn them into ids, and stops with the allowed values if a name or code is not offered. [DECISIONS.md](DECISIONS.md) has the details. The Integration Service starts only after setup has finished without error, and it logs in to Fineract as `integration-api`, never as the admin.

It is safe to run again. A second run creates nothing:

```bash
docker compose run --rm setup
```

On a clean stack on 2026-10-13, the first run printed `created` for every step and `13 SYNCED (13 sent to Fineract)`. The second run printed `exists` for every step and `0 sent to Fineract`. Fineract's command log (`m_portfolio_command_source`) held 18 rows before and after the second run. `integration-service/tests/unit/test_setup_run.py` checks that a second run writes nothing new.

## Try the APIs in the browser

1. Open http://localhost:8080/fineract-provider/swagger-ui/index.html
2. Pick a spec in the **Select a definition** list at the top right:
   - **Fineract 1.15.0**: all of Fineract's own APIs
   - **ERP integration (glsync plugin)**: my plugin's `/v1/erp-integration/accounts` endpoints
3. Click **Authorize**. Fill in both:
   - `basicAuth`: user `mifos`, password `password`
   - `tenantid`: `default`
4. Open an endpoint, click **Try it out**, then **Execute**.

Without step 3, every call gets `401`.

## The Mock ERP in the browser

It listens on http://localhost:8090. Only this computer can reach it.

| What | Where |
|---|---|
| Small web UI: chart of accounts, journal, new entry and fault switches | http://localhost:8090/ (opens `/ui/accounts`; the others are `/ui/journal`, `/ui/journal/new` and `/ui/faults`) |
| Swagger UI | http://localhost:8090/openapi/swagger |
| OpenAPI spec | http://localhost:8090/openapi/openapi.json |
| Health check | http://localhost:8090/health |

In Swagger, click **Authorize** and enter the API key from `.env` (`ERP_API_KEY`, by default `change-me-erp-key`). Calls to `/api` and `/admin` need it. The UI does not.

## The Integration Service in the browser

It listens on http://localhost:8091. Only this computer can reach it.

| What | Where |
|---|---|
| Swagger UI | http://localhost:8091/openapi/swagger |
| OpenAPI spec | http://localhost:8091/openapi/openapi.json |
| Health check | http://localhost:8091/health |

In Swagger, click **Authorize** and enter a partner key. Keys are set in `.env` as `PARTNER_API_KEYS`, in the form `name:key`. The default is `demo-partner:change-me-partner-key`. The routes are GL sync (`/api/accounts/...`), clients (`/api/clients`), loans with approve, disburse and repayments (`/api/loans/...`), the forms for each step (`/api/clients/template`, `/api/loans/template`, `/api/loans/{loan_reference}/template?step=`), `GET /api/transactions/{external_reference}`, `POST /api/reconciliation` and `POST /admin/recover`. See [API.md](API.md). A Postman collection with every partner endpoint is in `postman/collection.json`. Set its `base_url` and `api_key` variables.

The background loop that sends journal entries to the ERP and finishes stuck requests has three optional settings in `.env`: `INTEGRATION_OUTBOX_ENABLED` (default `true`), `INTEGRATION_OUTBOX_POLL_SECONDS` (default `5`) and `INTEGRATION_OUTBOX_MAX_ATTEMPTS` (default `8`).

## Tests

| Language | Command | What it runs |
|---|---|---|
| Java | `make test-java` | Builds Fineract (51 unit tests run during the build), starts it, then runs 10 integration tests against it. On 2026-10-14 the 10 integration tests passed against the running stack. Details: [fineract-customization/BUILD.md](fineract-customization/BUILD.md) |
| Python | `make test-python` | For the Mock ERP and then the Integration Service: ruff (lint and format), mypy in strict mode, then pytest. Last full run on 2026-10-14: Mock ERP 216 passed, Integration Service 925 passed, with ruff and mypy clean in both. |

The Python tests need the running stack and two empty test databases: `erp_test` and `integration_test`. They rebuild their tables on every run. A new data volume creates both by itself. On an older volume, create them once:

```bash
docker compose exec db createdb -U postgres -O erp erp_test
docker compose exec db createdb -U postgres -O integration integration_test
```

To use other databases, set `ERP_TEST_DATABASE_URL` or `INTEGRATION_TEST_DATABASE_URL`, for example `postgresql+psycopg://integration:change-me-integration@localhost:5432/integration_test`.

Some Integration Service tests call the live Fineract and Mock ERP. They log in to Fineract as the API user. `tests/conftest.py` reads `FINERACT_API_USERNAME` and `FINERACT_API_PASSWORD` from `.env` when they are not already set. They never assume Fineract is empty and never delete anything. They use new GL codes (8 digits, starting with 7), new client ids and new references on every run, so they can run again and again. This leaves test clients, loans and accounts in Fineract.

Other commands: `make down` stops everything. `make reset` stops everything and deletes all data. `make help` lists all commands.

## Simulations

Each scenario has a `make` command. It prints the state before and after, in the brief's evidence format. Run them after `make up`. They only call the Integration Service, like a partner, and read Fineract, the ERP and the databases to show the state. Each run uses new ids, so they can run again.

| Command | ID | What it shows |
|---|---|---|
| `make demo-gl-sync` | A-1 | The plugin's four rules on `GL-1200`: one mapping, same data gives the same account, different data gives 409, no permission gives 403 |
| `make simulate-gl-sync-4x` | A-2 | Two new ERP accounts, each sent 4 times at once. Every count must be 1. |
| `make demo-identity` | C-1 | Create a client, find it by `client_uid`, check its name, send it again, send different data, reuse the `client_uid` |
| `make demo-lifecycle` | D-1 | A 5,000,000 RWF loan from application to Closed, with Fineract, ERP and service state after each step |
| `make simulate-duplicates` | I-1 | The same disbursement sent 4 times: 4 requests, 1 Fineract disbursement, 1 ERP entry, 1 operation |
| `make simulate-concurrent-disburse` | I-2a, I-2b | 10 disbursements of one loan at once, with one reference, then with ten |
| `make simulate-concurrent-repay` | I-3 | 10 repayments at once, 6 references plus copies |
| `make simulate-j1` | J-1 | The ERP is down after Fineract succeeds |
| `make simulate-j2` | J-2 | Fineract is down |
| `make simulate-j3` | J-3 | Fineract times out after the request was sent. Takes about 4 minutes. |
| `make simulate-j4` | J-4 | The ERP times out after it saved the entry |
| `make simulate-j5` | J-5 | The service crashes between Fineract and the ERP. It restarts the Integration Service container. |
| `make simulate-recon` | K-1 | Reconciliation with all four outcomes planted |

`make demo-gl-sync` creates one extra Fineract user the first time, `demo-no-sync`, with only `READ_GLACCOUNT`, to show the 403. `make simulate-gl-sync-4x` adds two new ERP accounts (codes starting with 7, under 5000) on every run. The J simulations switch faults on in the Mock ERP and the fault proxy, and switch every fault off at the end.

## Folders

```
├── README.md  ARCHITECTURE.md  API.md  SIMULATIONS.md  DECISIONS.md
├── Makefile
├── docker-compose.yml
├── docs/openapi/                  OpenAPI specs
├── infra/postgres/                creates one database per service
├── fineract-customization/
│   ├── fineract/                  Fineract 1.15.0 source (submodule, never edited)
│   ├── custom/inkomoko/glsync/    my Java plugin
│   ├── Dockerfile                 builds Fineract with the plugin
│   ├── patch/glsync.diff          my change as a diff against 1.15.0
│   └── BUILD.md, build.log, integration-test.log
├── fineract/configuration/        setup settings: office, currency, role, API user, loan product
├── integration-service/           Python: the Integration Service (Flask, Postgres) and the setup step
├── mock-erp/                      Python: the Mock ERP (Flask, Postgres, small web UI)
├── fault-proxy/                   Python: the fault proxy in front of Fineract (failure simulations only)
├── scripts/                       demos and simulations
└── postman/collection.json        every partner endpoint, with fake data
```

## Other documents

| File | What is in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The parts of the system and how data moves between them |
| [API.md](API.md) | Every endpoint, its rules and its errors |
| [SIMULATIONS.md](SIMULATIONS.md) | The failure tests and their results |
| [DECISIONS.md](DECISIONS.md) | The choices I made and why |
| [fineract-customization/BUILD.md](fineract-customization/BUILD.md) | How the Fineract image is built and tested |

## Use of AI

<!-- author: please review this section -->

The brief allows AI tools. I used Claude Code (Anthropic) heavily: to read the brief, design the system, write the code and the tests, run the simulations and draft these documents. I directed the work, made the decisions and reviewed the results. I can explain and change any part.

Every result in these documents was copied from a real run. The raw output is not kept in the repo. The simulations can be run again with their `make` commands. Where an example comes from a run with no `make` command, the document says so. All data is fake: test names, made-up ids and references like `FIN-2026-000001`.

## Not done

- Part L (stretch) is not built. [DECISIONS.md](DECISIONS.md) lists what I chose not to build, and why.
- I did not rehearse the whole thing from a fresh `git clone` on another machine. Each part was run on a clean stack (`docker compose down -v`) on my machine.
