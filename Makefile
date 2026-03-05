COMPOSE := docker compose --env-file .env
RUFF := .venv/bin/ruff
RUFF_TMP := /tmp/packtrack-test-venv/bin/ruff
PYTEST := .venv/bin/pytest
PYTEST_TMP := /tmp/packtrack-test-venv/bin/pytest
TEST_TIMEOUT ?= 180
FORMAT_TARGETS := api worker tests

.PHONY: up down build rebuild logs logs-api logs-worker migrate seed format format-changed format-file test health frontend-build backup-postgres restore-postgres backup-minio restore-minio

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

build:
	$(COMPOSE) build

rebuild:
	$(COMPOSE) build --no-cache

logs:
	$(COMPOSE) logs -f --tail=200

logs-api:
	$(COMPOSE) logs -f --tail=200 api

logs-worker:
	$(COMPOSE) logs -f --tail=200 worker

migrate:
	$(COMPOSE) run --rm api alembic upgrade head

seed:
	$(COMPOSE) run --rm api python -m scripts.seed_taxonomy

format:
	@targets="$(FILES)"; \
	ruff_cmd=""; \
	if [ -x "$(RUFF_TMP)" ]; then ruff_cmd="$(RUFF_TMP)"; \
	elif [ -x "$(RUFF)" ]; then ruff_cmd="$(RUFF)"; \
	fi; \
	if [ -z "$$targets" ]; then targets="$(FORMAT_TARGETS)"; fi; \
	if [ -n "$$ruff_cmd" ]; then "$$ruff_cmd" format $$targets; else ruff format $$targets; fi

format-changed:
	@if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
		echo "format-changed requires a git worktree."; \
		exit 1; \
	fi
	@ruff_cmd=""; \
	if [ -x "$(RUFF_TMP)" ]; then ruff_cmd="$(RUFF_TMP)"; \
	elif [ -x "$(RUFF)" ]; then ruff_cmd="$(RUFF)"; \
	fi; \
	changed_files="$$( { git diff --name-only -- '*.py'; git diff --name-only --cached -- '*.py'; } | awk 'NF' | sort -u )"; \
	if [ -z "$$changed_files" ]; then \
		echo "No changed Python files to format."; \
		exit 0; \
	fi; \
	if [ -n "$$ruff_cmd" ]; then "$$ruff_cmd" format $$changed_files; else ruff format $$changed_files; fi

format-file:
	@if [ -z "$(FILE)" ]; then \
		echo "Usage: make format-file FILE=path/to/file.py"; \
		exit 1; \
	fi
	@ruff_cmd=""; \
	if [ -x "$(RUFF_TMP)" ]; then ruff_cmd="$(RUFF_TMP)"; \
	elif [ -x "$(RUFF)" ]; then ruff_cmd="$(RUFF)"; \
	fi; \
	if [ -n "$$ruff_cmd" ]; then "$$ruff_cmd" format "$(FILE)"; else ruff format "$(FILE)"; fi

lint:
	@ruff_cmd=""; \
	if [ -x "$(RUFF_TMP)" ]; then ruff_cmd="$(RUFF_TMP)"; \
	elif [ -x "$(RUFF)" ]; then ruff_cmd="$(RUFF)"; \
	fi; \
	if [ -n "$$ruff_cmd" ]; then "$$ruff_cmd" check api worker tests; else ruff check api worker tests; fi

test:
	@timeout_cmd=""; \
	if command -v timeout >/dev/null 2>&1; then timeout_cmd="timeout $(TEST_TIMEOUT)s"; \
	elif command -v gtimeout >/dev/null 2>&1; then timeout_cmd="gtimeout $(TEST_TIMEOUT)s"; \
	fi; \
	if [ -x "$(PYTEST_TMP)" ]; then runner="$(PYTEST_TMP)"; \
	elif [ -x "$(PYTEST)" ]; then runner="$(PYTEST)"; \
	elif command -v pytest >/dev/null 2>&1; then runner="pytest"; \
	else echo "pytest not found"; exit 1; \
	fi; \
	echo "Running $$runner -q"; \
	if [ -n "$$timeout_cmd" ]; then $$timeout_cmd $$runner -q; else $$runner -q; fi

health:
	curl -fsS http://localhost:8000/api/v1/health && echo
	curl -fsS http://localhost:8001/api/v1/health && echo

frontend-build:
	$(COMPOSE) run --rm frontend npm run build

backup-postgres:
	./scripts/backup_postgres.sh

restore-postgres:
	@if [ -z "$(FILE)" ]; then \
		echo "Usage: make restore-postgres FILE=backups/<timestamp>/postgres.sql"; \
		exit 1; \
	fi
	./scripts/restore_postgres.sh "$(FILE)"

backup-minio:
	./scripts/backup_minio.sh

restore-minio:
	@if [ -z "$(FILE)" ]; then \
		echo "Usage: make restore-minio FILE=backups/<timestamp>/minio-data.tar.gz"; \
		exit 1; \
	fi
	./scripts/restore_minio.sh "$(FILE)"
