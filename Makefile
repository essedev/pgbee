.DEFAULT_GOAL := help

PG_PORT ?= 4460
export DATABASE_URL ?= postgresql://aidb:aidb@localhost:$(PG_PORT)/aidb

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install worker dependencies
	cd worker && uv sync

db-up: ## Start Postgres in Docker (port $(PG_PORT))
	docker compose -f demo/compose.yml up -d --wait

db-down: ## Stop Postgres
	docker compose -f demo/compose.yml down

db-install: ## Apply sql/ to the database
	cd worker && uv run pgbee install

worker: ## Run the worker loop
	cd worker && uv run pgbee run

status: ## Show derived columns and queue state
	cd worker && uv run pgbee status

demo: ## Seed the demo, declare derived columns, run the worker (asks before spending)
	cd worker && uv run python ../demo/run.py --reset

test: ## Run tests (needs db-up)
	cd worker && uv run pytest

test-llm: ## Run also the tests that call a real model (costs money)
	cd worker && uv run pytest -m 'not extension'

lint: ## Lint
	cd worker && uv run ruff check . ../demo

format: ## Format
	cd worker && uv run ruff format . ../demo

typecheck: ## Type check
	cd worker && uv run mypy src tests ../demo

check: ## Full quality pass (format, lint, typecheck, test)
	$(MAKE) format
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) test

worker-image: ## Build the worker image pgbee-worker:dev (configuration from the environment only)
	docker build -t pgbee-worker:dev worker

extension-image: ## Build the Postgres image with CREATE EXTENSION pgbee available (pgbee-postgres:dev)
	rm -rf worker/dist/extension
	cd worker && uv run pgbee extension-files dist/extension
	docker build -f docker/postgres/Dockerfile -t pgbee-postgres:dev worker/dist/extension

test-extension: extension-image ## Extension tests (create, update chain, dump and restore) on a throwaway container, port 4463
	docker rm -f pgbee-ext-test >/dev/null 2>&1 || true
	docker run -d --name pgbee-ext-test -e POSTGRES_USER=aidb -e POSTGRES_PASSWORD=aidb \
		-e POSTGRES_DB=aidb -p 4463:5432 pgbee-postgres:dev >/dev/null
	until docker exec pgbee-ext-test pg_isready -U aidb -h localhost >/dev/null 2>&1; do sleep 1; done
	cd worker && uv run pytest -m extension; status=$$?; docker rm -f pgbee-ext-test >/dev/null; exit $$status

build: ## Build the worker wheel and sdist (SQL files included) into worker/dist
	cd worker && rm -rf dist && uv build

clean: ## Remove build artifacts
	rm -rf worker/dist worker/.venv worker/.pytest_cache worker/.mypy_cache worker/.ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
