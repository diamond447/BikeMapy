# Local development

BikeMapy is a modular Django application with a separate React frontend. The
repository includes a Compose stack so a contributor does not need to install
PostgreSQL/PostGIS or Redis on their host.

## Requirements

- Docker Desktop or Docker Engine with Compose v2
- Python 3.13 and [uv](https://docs.astral.sh/uv/) (for editor tooling and
  host-side backend checks)
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
docker compose exec backend uv run --locked --no-dev python backend/manage.py migrate
docker compose exec worker uv run --locked --no-dev python -c 'from apps.ingestion.tasks import worker_smoke; print(worker_smoke.delay().get(timeout=10))'
```

The task should print `worker-ready`. Stop the stack with `docker compose down`;
use `docker compose down -v` only when intentionally removing local database,
Redis, and GPX volumes.

## BikeForum crawler

The crawler targets the public Bike-Forum origin `https://www.bike-forum.cz`;
the default incremental cursor is `https://www.bike-forum.cz/forum/`.
The crawler is deliberately bounded and resumable. It fetches only public
server-rendered HTML, identifies itself with `BIKEFORUM_USER_AGENT`, reads
`robots.txt`, waits between requests, caches responses, and uses bounded
retries. Requests and discovered links are restricted to the explicitly
configured `BIKEFORUM_ALLOWED_ORIGINS`; DNS resolution is checked for private
or link-local addresses immediately before each network request. This reduces
DNS rebinding risk, although a transport that cannot pin the resolved address
still has the normal DNS TOCTOU limitation. Keep the default two-second
request interval unless the forum owner has explicitly granted a different
limit.

Run an incremental crawl through Celery:

```sh
docker compose exec backend uv run --locked --no-dev python -c \
  'from apps.ingestion.tasks import crawl_bikeforum; print(crawl_bikeforum.delay().get(timeout=120))'
```

Compose runs a dedicated `beat` service alongside the worker. It dispatches
the scheduled crawl and retries durable quarantined-payload deletions every
15 minutes; the post-commit dispatch from moderation is therefore safe to
retry after a broker or storage outage.

Historical work must always have an explicit page bound:

```sh
docker compose exec backend uv run --locked --no-dev python -c \
  'from apps.ingestion.tasks import backfill_bikeforum; print(backfill_bikeforum.delay("https://www.bike-forum.cz/forum/", max_pages=20).get(timeout=120))'
```

`CrawlCheckpoint`, `CrawlTask`, and its leased `CrawlPageWork` frontier are the
source of truth for progress, so a worker interruption leaves queued or
expired work available for a later run. Listing pages enqueue thread pages;
each failed page is retained with its error while other frontier items
continue. The HTML parser, HTTP fetcher, and source availability checker are
explicit interfaces, with representative index/thread fixtures under
`backend/apps/ingestion/tests/fixtures/`.

To verify the real infrastructure path (PostGIS plus a task delivered through
Redis to a Celery worker), run:

```sh
make integration
```

The database and Redis ports are bound to `127.0.0.1` for optional host-side
inspection and are not exposed on the network.

## Host-side quality checks

Install dependencies once with `make install`. This creates the repository
`.venv` from the committed `uv.lock`; all backend commands in the Makefile run
through that locked environment:

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

Anonymous reporting also requires `REPORT_TURNSTILE_SECRET_KEY` and the public
frontend `VITE_TURNSTILE_SITE_KEY` for the Cloudflare challenge.
`REPORT_RATE_LIMIT_HMAC_SECRET` should be a separate deployment secret; when
omitted, Django's secret key is used as a safe local fallback. The defaults
allow three reports per hour and ten per day. The Celery beat service runs
closed-report retention daily.

Keep `REPORT_CLIENT_IP_MODE=direct` for local Compose. A production Cloudflare
Tunnel must configure its exact proxy CIDRs before selecting `cloudflare`;
never trust forwarded headers from arbitrary peers.
