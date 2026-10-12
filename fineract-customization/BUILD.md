# Building and testing the Fineract customisation

Everything builds inside Docker; no local JDK or Gradle is needed. Versions are pinned in [`VERSIONS`](VERSIONS).

## What gets built

The Dockerfile clones Apache Fineract tag `1.15.0`, fails unless it resolves to commit `d5636847`, copies our module into Fineract's `custom/` tree, and builds with the Gradle wrapper bundled with the tag. Our two jars go to `/app/libs` and are loaded at runtime by Spring Boot's `loader.path`. No core file is changed.

```
custom/inkomoko/glsync/
├── service/   resource, command handler, service, entity, 409 mapper, OpenAPI spec, tests
└── starter/   auto-configuration and Liquibase changelogs
```

## Commands

Run from the repository root.

| Step | Command |
|---|---|
| Build the image and run the unit tests | `docker buildx build --load --progress=plain -t fineract-inkomoko:1.15.0 fineract-customization 2>&1 \| tee fineract-customization/build.log` |
| Start Postgres and Fineract | `cp .env.example .env && docker compose up -d --no-build --wait` |
| Build the integration test image (seconds; reuses the cached build stage) | `docker buildx build --load --target integration-test -t fineract-inkomoko-it:1.15.0 fineract-customization` |
| Run the integration tests against the running stack | `docker run --rm --network inkomoko-fineract_default -e FINERACT_URL=http://fineract:8080 fineract-inkomoko-it:1.15.0 \| tee fineract-customization/integration-test.log` |

A failing unit test fails the image build. The integration tests only speak HTTP to Fineract, so the image build packages the compiled tests with JUnit, AssertJ and Gson into a small image that runs on the compose network, with no Gradle at test time. Each run uses fresh references and GL codes, so it can be repeated against the same database.

## Tests

| Kind | Class | What it proves |
|---|---|---|
| Unit | `PayloadHasherTest` | "Same payload" ignores key order, whitespace, nulls and `1` vs `"1"`; a changed value changes the hash |
| Unit | `ErpAccountCommandsTest` | The command is `GLACCOUNT`/`SYNC` guarded by `SYNC_GLACCOUNT`; reference validation; `usage` is required |
| Unit | `GlAccountSyncServiceTest` | Create, unchanged, payload conflict, GL-code conflict, trimming, and locks taken before any read |
| Unit | `AdvisoryLocksTest` | Locks are taken in sorted order, so two requests can't deadlock |
| Integration | `ErpAccountsApiIntegrationTest` | Against real Fineract and Postgres: behaviours 1–4, GL-code conflict, 403 without permission, 10 parallel syncs create one account, `Idempotency-Key` replay, 404 and 400 cases |

## Evidence

| File | Contents |
|---|---|
| `build.log` | Image build from the pinned tag, with every unit test result |
| `integration-test.log` | Integration test results against the running image |
| `patch/glsync.diff` | `git diff 1.15.0` with the module added: 30 new files, 1,399 lines, nothing outside `custom/inkomoko/`. Check with `git apply --check patch/glsync.diff` on a clean 1.15.0 checkout. |
| `../docs/openapi/fineract-erp-accounts.yaml` | The module's OpenAPI spec, as served at `/fineract-provider/glsync.json` |

## Developing in an IDE

`fineract/` is the 1.15.0 submodule. Symlink the module into its `custom/` tree and hide the link from git:

```bash
ln -s ../../custom/inkomoko fineract-customization/fineract/custom/inkomoko
echo "custom/inkomoko" >> .git/modules/fineract-upstream/info/exclude
```

Then open `fineract-customization/fineract` as a Gradle project in IntelliJ (JDK 21).
