SHELL := /bin/sh
UV ?= uv

.PHONY: install dev lint typecheck test check build format api-schema worker-smoke integration

install:
	$(UV) sync --locked --extra dev
	cd frontend && corepack pnpm install --frozen-lockfile

dev:
	docker compose up --build

lint:
	$(UV) run --locked --extra dev ruff check backend scripts
	$(UV) run --locked --extra dev ruff format --check backend scripts
	cd frontend && corepack pnpm lint && corepack pnpm format

typecheck:
	DJANGO_DATABASE_ENGINE=django.db.backends.sqlite3 $(UV) run --locked --extra dev mypy backend
	cd frontend && corepack pnpm exec tsc -b --pretty false

test:
	DJANGO_DATABASE_ENGINE=django.db.backends.sqlite3 $(UV) run --locked --extra dev pytest
	cd frontend && corepack pnpm test

check: lint typecheck test build

build:
	cd frontend && corepack pnpm build

format:
	$(UV) run --locked --extra dev ruff format backend
	cd frontend && corepack pnpm format:write

api-schema:
	$(UV) run --locked --extra dev python backend/manage.py spectacular --file openapi.yaml --validate
	cd frontend && corepack pnpm api:generate

worker-smoke:
	$(UV) run --locked --extra dev python -c 'from apps.ingestion.tasks import worker_smoke; assert worker_smoke.apply().get() == "worker-ready"'

integration:
	docker compose up -d --build db redis backend worker
	docker compose exec backend uv run --locked --no-dev python backend/manage.py migrate --noinput
	docker compose exec backend uv run --locked --no-dev python scripts/check_integrations.py
