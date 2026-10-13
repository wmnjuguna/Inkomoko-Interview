SHELL := /bin/bash
.SHELLFLAGS := -eo pipefail -c

FINERACT_IMAGE := fineract-inkomoko:1.15.0
FINERACT_IT_IMAGE := fineract-inkomoko-it:1.15.0
MOCK_ERP_IMAGE := mock-erp-inkomoko:0.1.0
COMPOSE_NETWORK := inkomoko-fineract_default

.PHONY: help build up down reset test-java test-python

help:
	@echo "build        build the Fineract image from source (runs the Java unit tests), the integration test image and the Mock ERP image, and refresh both OpenAPI specs"
	@echo "up           start everything (creates .env from .env.example on first run)"
	@echo "down         stop containers, keep data"
	@echo "reset        stop containers and delete all data"
	@echo "test-java    Java unit tests (image build) + integration tests against the running stack"
	@echo "test-python  Mock ERP: ruff, mypy and pytest (needs the db container and an erp_test database)"

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
