SHELL := /bin/sh
PYTHON ?= python3

.PHONY: install dev lint typecheck test check build format api-schema worker-smoke integration

install:
	$(PYTHON) -m pip install -e '.[dev]'
	cd frontend && corepack pnpm install --frozen-lockfile

dev:
	docker compose up --build

lint:
	ruff check backend scripts
	ruff format --check backend scripts
	cd frontend && corepack pnpm lint && corepack pnpm format

typecheck:
	DJANGO_DATABASE_ENGINE=django.db.backends.sqlite3 mypy backend
	cd frontend && corepack pnpm exec tsc -b --pretty false

test:
	DJANGO_DATABASE_ENGINE=django.db.backends.sqlite3 pytest
	cd frontend && corepack pnpm test

check: lint typecheck test build

build:
	cd frontend && corepack pnpm build

format:
	ruff format backend
	cd frontend && corepack pnpm format:write

api-schema:
	$(PYTHON) backend/manage.py spectacular --file openapi.yaml --validate

worker-smoke:
	PYTHONPATH=backend $(PYTHON) -c 'from apps.ingestion.tasks import worker_smoke; assert worker_smoke.apply().get() == "worker-ready"'

integration:
	docker compose up -d --build db redis backend worker
	docker compose exec backend python backend/manage.py migrate --noinput
	docker compose exec backend python scripts/check_integrations.py
