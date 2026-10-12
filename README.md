# Fineract ↔ ERP Integration — Take-Home Assessment

Apache Fineract (Java/Spring Boot) customisation + Python Mock ERP + Python Integration Service, built to show that money moves exactly once under duplicates, timeouts, partial failure and concurrency.

> **Status:** design and scaffolding. Nothing below marked *planned* exists yet. This README is updated as each part lands, and the tier table only claims what `evidence/` shows.

## Tiers

| Tier | Parts | Status |
|---|---|---|
| 1. Core (required) | A–G | Not started |
| 2. Reliability (required) | H–K | Not started |
| 3. Stretch (optional) | L | Not started. Candidates: GL master-data reconciliation, dead-letter view |

## Pinned versions

| Component | Version | Source of truth |
|---|---|---|
| Apache Fineract | `1.15.0` (commit `d5636847ac556c30b437254c353f05526d172b97`) | git submodule `fineract-customization/fineract` |
| JDK | 21 | Fineract `build.gradle` toolchain (`JavaLanguageVersion.of(21)`) |
| Build tool | Gradle 8.14.5 (wrapper) | `gradle/wrapper/gradle-wrapper.properties` in the tag |
| Database | PostgreSQL 18.3 | Same image upstream 1.15.0 uses in `config/docker/compose/postgresql.yml` |
| Python | 3.12 *(to confirm when the services are scaffolded)* | `pyproject.toml` |

## Getting the code

```bash
git clone --recurse-submodules <this-repo>
# or, in an existing clone:
git submodule update --init --depth 1
git -C fineract-customization/fineract describe --tags   # → 1.15.0
```

`fineract-customization/fineract` is the unmodified upstream tag. Our Java changes live beside it in `fineract-customization/` and are copied into Fineract's `custom/` module tree at build time (see [ARCHITECTURE.md](ARCHITECTURE.md#fineract-customisation)).

## Running *(planned)*

```bash
make build && make up            # or: cp .env.example .env && docker compose up --build
```

The bootstrap step (office, currency, GL sync, loan product, API user) runs as a one-shot service and is safe to re-run.

## Tests

| Language | Command | Covers |
|---|---|---|
| Java | `make test-java` | Builds Fineract from source (43 unit tests run inside the build), starts the stack, runs 8 integration tests against it. Underlying commands: [fineract-customization/BUILD.md](fineract-customization/BUILD.md) |
| Python | *(planned)* `make test-python` | ruff, mypy, pytest: unit, integration, failure, concurrency |

Other targets: `make build`, `make up`, `make down`, `make reset` (deletes all data). `make help` lists them.

## Simulations *(planned)*

`make simulate-<name>` prints before / during / after state using the evidence template. The full list is in [SIMULATIONS.md](SIMULATIONS.md).

## Repository layout

```
├── README.md  ARCHITECTURE.md  API.md  SIMULATIONS.md  DECISIONS.md
├── fineract-customization/
│   ├── fineract/                 # submodule: apache/fineract @ 1.15.0 (never edited)
│   ├── custom/inkomoko/…         # our Fineract modules (planned)
│   ├── Dockerfile                # 1.15.0 + our modules → custom image (planned)
│   ├── patch/                    # git diff against 1.15.0 (generated)
│   └── BUILD.md, build.log       # build evidence
├── fineract/configuration/       # office, currency, product, roles (planned)
├── integration-service/          # Python (planned)
├── mock-erp/                     # Python (planned)
├── scripts/                      # bootstrap, demos, simulations (planned)
├── postman/collection.json       # supporting evidence only (planned)
└── evidence/                     # captured outputs (planned)
```

## Documentation

| File | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, data model, state machine, GL sync design |
| [API.md](API.md) | Endpoints, examples, error format, idempotency contract |
| [SIMULATIONS.md](SIMULATIONS.md) | Every simulation, using the evidence template |
| [DECISIONS.md](DECISIONS.md) | Trade-offs, assumptions, what we didn't build, known weaknesses |

## AI usage disclosure

AI assistants were used on this submission, as the brief allows. So far, Claude was used to review the requirements, draft the architecture brief, and scaffold this repository and its documentation. Every part will be explainable and modifiable live. This section is updated as work continues.

## If time runs out

What's left and why it's next will be listed here, in priority order.
