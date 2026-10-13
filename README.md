# Fineract and ERP integration

This is my take-home assessment. It connects three things:

- **Apache Fineract**, the core banking system. I changed it with a small Java plugin.
- **A mock ERP**, written in Python. It owns the chart of accounts.
- **An integration service**, written in Python. It sits between partners, Fineract and the ERP.

The goal is to show that money moves exactly once, even with duplicate requests, timeouts, crashes and requests that arrive at the same time.

> **Status:** the Fineract part of Tier 1 Part A is built and tested. The Mock ERP is built and tested. The Integration Service is not started yet. Anything marked *planned* does not exist yet.

## What is done

| Tier | Parts | Status |
|---|---|---|
| 1. Core (required) | A–G | Part A, Java side: done and tested. The Mock ERP: done and tested. Parts B–G: not started. |
| 2. Reliability (required) | H–K | Not started |
| 3. Stretch (optional) | L | Not started. Ideas: GL master-data reconciliation, a dead-letter view |

## Versions

| What | Version | Where it is pinned |
|---|---|---|
| Apache Fineract | `1.15.0`, commit `d5636847ac556c30b437254c353f05526d172b97` | git submodule `fineract-customization/fineract` |
| JDK | 21 | Fineract's own build file |
| Build tool | Gradle 8.14.5 | the Gradle wrapper inside the tag |
| Database | PostgreSQL 18.3 (Alpine image) | `docker-compose.yml` |
| Python | 3.13 (image `python:3.13.16-alpine3.24`) | `mock-erp/pyproject.toml`, `mock-erp/Dockerfile` |
| Python packages | Flask 3, flask-openapi3, Pydantic 2, SQLAlchemy 2, Alembic, psycopg 3, Gunicorn | `mock-erp/uv.lock` |

## Get the code

```bash
git clone --recurse-submodules <this-repo>
# or, if you already cloned without submodules:
git submodule update --init --depth 1
```

`fineract-customization/fineract` is the original Fineract 1.15.0 source. I never edit it. My Java code lives next to it, in `fineract-customization/custom/`. The build copies it into Fineract.

## Run it

```bash
make build    # builds Fineract from source and runs the Java unit tests, then builds the Mock ERP
make up       # starts Postgres, Fineract and the Mock ERP
```

`make up` creates `.env` from `.env.example` the first time. Fineract takes about two minutes to start. The Mock ERP starts in a few seconds.

To rebuild only the Mock ERP:

```bash
docker buildx build --load -t mock-erp-inkomoko:0.1.0 mock-erp
docker compose up -d --no-build --wait mock-erp
```

The Mock ERP updates its own database tables when it starts. It also adds a small starting chart of accounts.

*(planned)* A setup step will create the office, the currency, the loan product and the API user. It will be safe to run more than once.

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
| Small web UI: chart of accounts and fault switches | http://localhost:8090/ (opens `/ui/accounts`; the faults page is `/ui/faults`) |
| Swagger UI | http://localhost:8090/openapi/swagger |
| OpenAPI spec | http://localhost:8090/openapi/openapi.json |
| Health check | http://localhost:8090/health |

In Swagger, click **Authorize** and enter the API key from `.env` (`ERP_API_KEY`, by default `change-me-erp-key`). Calls to `/api` and `/admin` need it. The UI does not.

## Tests

| Language | Command | What it runs |
|---|---|---|
| Java | `make test-java` | Builds Fineract (51 unit tests run during the build), starts it, then runs 10 integration tests against it. Details: [fineract-customization/BUILD.md](fineract-customization/BUILD.md) |
| Python | `make test-python` | Mock ERP: ruff (lint and format), mypy in strict mode, then pytest. Last run: 195 tests passed. |

The Python tests need the `db` container running and an empty `erp_test` database. They rebuild its tables on every run. A new data volume creates `erp_test` by itself. On an older volume, create it once:

```bash
docker compose exec db createdb -U postgres -O erp erp_test
```

To use another database, set `ERP_TEST_DATABASE_URL`, for example `postgresql+psycopg://erp:change-me-erp@localhost:5432/erp_test`.

Other commands: `make down` stops everything. `make reset` stops everything and deletes all data. `make help` lists all commands.

## Simulations *(planned)*

Each failure scenario will have a command like `make simulate-duplicates`. It will print the state before, during and after. The list is in [SIMULATIONS.md](SIMULATIONS.md).

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
├── integration-service/           Python (planned)
├── mock-erp/                      Python: the Mock ERP (Flask, Postgres, small web UI)
└── scripts/                       setup and simulations (planned)
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

The brief allows AI tools. I used Claude to read the requirements, plan the design, write code and tests, and write these documents. I reviewed all of it and can explain and change any part.

## If I run out of time

I will list here what is left and why, most important first.
