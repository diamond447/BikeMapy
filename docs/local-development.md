# Local development

BikeMapy is a modular Django application with a separate React frontend. The
repository includes a Compose stack so a contributor does not need to install
PostgreSQL/PostGIS or Redis on their host.

## Requirements

- Docker Desktop or Docker Engine with Compose v2
- Python 3.13 (for editor tooling and optional host-side checks)
- Node.js 22 with Corepack (for optional host-side frontend checks)

## Start the stack

```sh
cp .env.example .env
docker compose up --build
```

The frontend is available at <http://localhost:5173>, the backend at
<http://localhost:8000>, and the API documentation at
<http://localhost:8000/api/docs/>. The PostGIS database and Redis are private
service dependencies; their data is retained in named Docker volumes.

In another terminal, apply migrations and run the worker smoke test:

```sh
docker compose exec backend python backend/manage.py migrate
docker compose exec worker python -c 'from apps.ingestion.tasks import worker_smoke; print(worker_smoke.delay().get(timeout=10))'
```

The task should print `worker-ready`. Stop the stack with `docker compose down`;
use `docker compose down -v` only when intentionally removing local database,
Redis, and GPX volumes.

To verify the real infrastructure path (PostGIS plus a task delivered through
Redis to a Celery worker), run:

```sh
make integration
```

The database and Redis ports are bound to `127.0.0.1` for optional host-side
inspection and are not exposed on the network.

## Host-side quality checks

Install dependencies once with `make install`, then use the common targets:

```sh
make lint       # Ruff, ESLint, and Prettier checks
make typecheck  # mypy with django-stubs and TypeScript
make test       # pytest and Vitest (80% coverage floor)
make check      # all of the above
```

Playwright's browser binaries are installed separately when end-to-end tests
are needed: `cd frontend && pnpm exec playwright install --with-deps chromium`.

The OpenAPI client destination is `frontend/src/api/generated/`. Generate it
after the backend is running with `make api-schema` followed by
`cd frontend && pnpm api:generate`. Generated output is intentionally kept
out of the initial skeleton until the API contract is established.

## Configuration and secrets

`.env.example` and `frontend/.env.example` contain placeholders only. Copy
them locally; never commit `.env`, credentials, OAuth secrets, or production
configuration. A production deployment must provide a strong secret key and
explicit hosts, database credentials, allowed origins, and secure cookie/TLS
settings through its environment.
