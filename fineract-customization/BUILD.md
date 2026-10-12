# Building and testing the Fineract plugin

Everything is built inside Docker. You do not need Java or Gradle on your machine. The exact versions are in [`VERSIONS`](VERSIONS).

## What the build does

1. Downloads Apache Fineract `1.15.0`.
2. Checks that it is exactly commit `d5636847`. If not, the build stops.
3. Copies my plugin into Fineract's `custom/` folder.
4. Runs the plugin's unit tests. If one fails, the build stops.
5. Builds Fineract and the two plugin jars.
6. Puts it all in an image. The plugin jars sit in `/app/libs`, and Fineract loads them when it starts.

No Fineract source file is changed.

```
custom/inkomoko/glsync/
├── service/   the API, the rules, the table code, the errors, the OpenAPI spec, the tests
└── starter/   switches the plugin on, and the database migrations
```

## Commands

Run these from the repository root.

| What | Command |
|---|---|
| Build everything and run the unit tests | `make build` |
| Start Postgres and Fineract | `make up` |
| Run all Java tests (builds and starts first) | `make test-java` |

What `make` runs underneath:

| Step | Command |
|---|---|
| Build the image | `docker buildx build --load -t fineract-inkomoko:1.15.0 fineract-customization` |
| Build the test image (takes seconds, reuses the build) | `docker buildx build --load --target integration-test -t fineract-inkomoko-it:1.15.0 fineract-customization` |
| Start | `docker compose up -d --no-build --wait` |
| Run the integration tests | `docker run --rm --network inkomoko-fineract_default -e FINERACT_URL=http://fineract:8080 fineract-inkomoko-it:1.15.0` |

The integration tests only talk to Fineract over HTTP. So they run as a small container on the same network, with no Gradle. A run takes a few seconds. Each run uses new references and GL codes, so you can run it again on the same database.

`make build` also copies the OpenAPI spec out of the plugin jar into `docs/openapi/fineract-erp-accounts.yaml`. So the copy in the repo always matches the build.

## Tests

| Kind | Class | What it checks |
|---|---|---|
| Unit | `PayloadHasherTest` | "Same data" ignores key order, extra spaces, empty fields, and `1` vs `"1"`. Changed data gives a different hash. |
| Unit | `ErpAccountCommandsTest` | The request becomes a Fineract `GLACCOUNT`/`SYNC` command that needs `SYNC_GLACCOUNT`. Bad references, bad `type` or `usage`, Fineract's `parentId`, and an account that is its own parent are all rejected, and the error names the right field. |
| Unit | `GlAccountSyncServiceTest` | Create, unchanged, data conflict, GL code conflict, parent rules, and that locks are taken before anything is read |
| Unit | `AdvisoryLocksTest` | Locks are always taken in the same order, so two requests cannot block each other forever |
| Integration | `ErpAccountsApiIntegrationTest` | Against real Fineract and Postgres: the four Part A behaviours, GL code conflict, bad types and usages, parent accounts and the hierarchy, 403 without permission, 10 requests at once create one account, `Idempotency-Key` replay, 404 and 400 |

Totals: 51 unit tests and 10 integration tests.

## Evidence files

| File | What it shows |
|---|---|
| `build.log` | The image build from the pinned tag, with every unit test result |
| `integration-test.log` | The integration test results against the running image |
| `patch/glsync.diff` | My change as a diff against 1.15.0: 30 new files, 1,793 lines, nothing outside `custom/inkomoko/`. Check it with `git apply --check patch/glsync.diff` on a clean 1.15.0 checkout. |
| `../docs/openapi/fineract-erp-accounts.yaml` | The plugin's OpenAPI spec |

## Working in an IDE

`fineract/` is the Fineract 1.15.0 submodule. Link the plugin into it, and tell git to ignore the link:

```bash
ln -s ../../custom/inkomoko fineract-customization/fineract/custom/inkomoko
echo "custom/inkomoko" >> .git/modules/fineract-upstream/info/exclude
```

Then open `fineract-customization/fineract` in IntelliJ as a Gradle project, with JDK 21.
