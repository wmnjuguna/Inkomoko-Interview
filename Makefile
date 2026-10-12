SHELL := /bin/bash
.SHELLFLAGS := -eo pipefail -c

FINERACT_IMAGE := fineract-inkomoko:1.15.0
FINERACT_IT_IMAGE := fineract-inkomoko-it:1.15.0
COMPOSE_NETWORK := inkomoko-fineract_default

.PHONY: help build up down reset test-java

help:
	@echo "build      build the Fineract image from source (runs the Java unit tests), the integration test image, and copy the OpenAPI spec"
	@echo "up         start everything (creates .env from .env.example on first run)"
	@echo "down       stop containers, keep data"
	@echo "reset      stop containers and delete all data"
	@echo "test-java  Java unit tests (image build) + integration tests against the running stack"

.env:
	cp .env.example .env

build: .env
	docker buildx build --load --progress=plain -t $(FINERACT_IMAGE) fineract-customization 2>&1 | tee fineract-customization/build.log
	docker buildx build --load --target integration-test -t $(FINERACT_IT_IMAGE) fineract-customization
	@# Copy the OpenAPI spec the build generated, straight from the plugin jar, so the repo copy always matches.
	docker run --rm --entrypoint unzip $(FINERACT_IMAGE) -p /app/libs/inkomoko-fineract-glsync-service-1.15.0.jar static/glsync.yaml > docs/openapi/fineract-erp-integration.yaml

up: .env
	docker compose up -d --no-build --wait

down:
	docker compose down

reset:
	docker compose down -v

test-java: build up
	docker run --rm --network $(COMPOSE_NETWORK) -e FINERACT_URL=http://fineract:8080 $(FINERACT_IT_IMAGE) | tee fineract-customization/integration-test.log
