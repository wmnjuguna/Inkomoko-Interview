SHELL := /bin/bash
.SHELLFLAGS := -eo pipefail -c

FINERACT_IMAGE := fineract-inkomoko:1.15.0
FINERACT_IT_IMAGE := fineract-inkomoko-it:1.15.0
MOCK_ERP_IMAGE := mock-erp-inkomoko:0.1.0
INTEGRATION_IMAGE := integration-service-inkomoko:0.1.0
COMPOSE_NETWORK := inkomoko-fineract_default

.PHONY: help build up down reset test-java test-python demo-gl-sync simulate-gl-sync-4x demo-identity demo-lifecycle simulate-duplicates

help:
	@echo "build        build the Fineract image from source (runs the Java unit tests), the integration test image, the Mock ERP and Integration Service images, and refresh the OpenAPI specs"
	@echo "up           start everything (creates .env from .env.example on first run)"
	@echo "down         stop containers, keep data"
	@echo "reset        stop containers and delete all data"
	@echo "test-java    Java unit tests (image build) + integration tests against the running stack"
	@echo "test-python  Mock ERP and Integration Service: ruff, mypy and pytest (needs the running stack and the test databases)"
	@echo "demo-gl-sync         Part A behaviours 1-4 on the running stack, in the evidence format"
	@echo "simulate-gl-sync-4x  a new ERP account sent 4 times at once (service and plugin); every count must be 1"
	@echo "demo-identity        C-1: create a client, find it by client_uid, check it, replay, conflict"
	@echo "demo-lifecycle       D-1: a 5,000,000 RWF loan from application to Closed, with Fineract and ERP state after each stage"
	@echo "simulate-duplicates  I-1: the same disbursement sent 4 times; requests 4, Fineract 1, ERP 1, operations 1"

.env:
	cp .env.example .env

build: .env
	docker buildx build --load --progress=plain -t $(FINERACT_IMAGE) fineract-customization 2>&1 | tee fineract-customization/build.log
	docker buildx build --load --target integration-test -t $(FINERACT_IT_IMAGE) fineract-customization
	@# Copy the OpenAPI spec the build generated, straight from the plugin jar, so the repo copy always matches.
	docker run --rm --entrypoint unzip $(FINERACT_IMAGE) -p /app/libs/inkomoko-fineract-glsync-service-1.15.0.jar static/glsync.yaml > docs/openapi/fineract-erp-integration.yaml
	docker buildx build --load -t $(MOCK_ERP_IMAGE) mock-erp
	@# Generate the Mock ERP spec from the app inside the image (dummy settings, no database needed), then turn the JSON into YAML.
	docker run --rm --entrypoint python -e ERP_DATABASE_URL=postgresql+psycopg://unused@localhost/unused -e ERP_API_KEY=unused $(MOCK_ERP_IMAGE) \
		-c 'import json; from mock_erp.app import create_app; print(json.dumps(create_app().api_doc))' \
		| uv run --no-project --quiet --with pyyaml python -c 'import json, sys, yaml; sys.stdout.write(yaml.safe_dump(json.load(sys.stdin), sort_keys=False, allow_unicode=True, width=100))' \
		> docs/openapi/mock-erp.yaml
	docker buildx build --load -t $(INTEGRATION_IMAGE) integration-service
	@# Same for the Integration Service: dummy settings, nothing is called while building the spec.
	@# OUTBOX_ENABLED=false: no background loop, so no log line ends up in the JSON.
	docker run --rm --entrypoint python -e OUTBOX_ENABLED=false \
		-e INTEGRATION_DATABASE_URL=postgresql+psycopg://unused@localhost/unused -e PARTNER_API_KEYS=unused:unused \
		-e FINERACT_BASE_URL=http://unused/fineract-provider/api/v1 -e FINERACT_USERNAME=unused -e FINERACT_PASSWORD=unused \
		-e ERP_BASE_URL=http://unused -e ERP_API_KEY=unused $(INTEGRATION_IMAGE) \
		-c 'import json; from integration_service.app import create_app; print(json.dumps(create_app().api_doc))' \
		| uv run --no-project --quiet --with pyyaml python -c 'import json, sys, yaml; sys.stdout.write(yaml.safe_dump(json.load(sys.stdin), sort_keys=False, allow_unicode=True, width=100))' \
		> docs/openapi/integration-service.yaml

up: .env
	docker compose up -d --no-build --wait

down:
	docker compose down

reset:
	docker compose down -v

test-java: build up
	docker run --rm --network $(COMPOSE_NETWORK) -e FINERACT_URL=http://fineract:8080 $(FINERACT_IT_IMAGE) | tee fineract-customization/integration-test.log

test-python:
	cd mock-erp && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
	cd integration-service && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q

# Part A evidence on the running stack (run setup first: make up). Uses the Integration Service venv.
demo-gl-sync: .env
	uv run --project integration-service python scripts/demo_gl_sync.py

simulate-gl-sync-4x: .env
	uv run --project integration-service python scripts/simulate_gl_sync_4x.py

# Parts C, D and I through the Integration Service, like a partner (run setup first: make up).
demo-identity: .env
	uv run --project integration-service python scripts/demo_identity.py

demo-lifecycle: .env
	uv run --project integration-service python scripts/demo_lifecycle.py

simulate-duplicates: .env
	uv run --project integration-service python scripts/simulate_duplicates.py
