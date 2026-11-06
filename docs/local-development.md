# Local development

## Requirements

- Docker with Compose v2
- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- Node.js 22 with Corepack

## Start the stack

```sh
cp .env.example .env
docker compose up --build
docker compose exec backend python backend/manage.py migrate
```

| Service | URL |
| --- | --- |
| Frontend | <http://localhost:5173> |
| API | <http://localhost:8000> |
| API documentation | <http://localhost:8000/api/docs/> |

PostgreSQL and Redis listen on `127.0.0.1` only and keep their data in named
volumes; `docker compose down -v` deletes them. To check that Celery receives
tasks through Redis:

```sh
docker compose exec worker python -c 'from apps.ingestion.tasks import worker_smoke; print(worker_smoke.delay().get(timeout=10))'
```

It prints `worker-ready`. `make integration` runs the same check against
PostGIS.

## Checks

```sh
make install    # .venv from uv.lock and frontend packages
make lint       # Ruff, ESLint, Prettier
make typecheck  # mypy, TypeScript
make test       # pytest (SQLite) and Vitest, 80 % coverage floor
make check      # all of the above and the frontend build
```

CI runs the full backend suite on PostGIS. To do the same locally, start a
`postgis/postgis:17-3.5` container and run pytest with
`DJANGO_DATABASE_ENGINE=django.contrib.gis.db.backends.postgis` and the
`POSTGRES_*` variables. Browser tests need Chromium once:
`cd frontend && pnpm exec playwright install --with-deps chromium`.

After an API change, regenerate the TypeScript client with
`make api-schema` and commit `frontend/src/api/generated/`.

## Crawler and GPX import

Celery beat runs a daily incremental crawl of the real forum, and found sources
are imported from Mapy.com. A bounded crawl can also be started by hand:

```sh
docker compose exec backend python -c \
  'from apps.ingestion.tasks import backfill_bikeforum; print(backfill_bikeforum.delay("https://www.bike-forum.cz/forum/", max_pages=20).get(timeout=120))'
```

Keep the default two-second request interval unless the forum owner agrees to
another. Parser fixtures are in `backend/apps/ingestion/tests/fixtures/`.

## Configuration

`.env.example` and `frontend/.env.example` list every setting with
placeholder values; never commit a filled-in `.env`. Reports need
`REPORT_TURNSTILE_SECRET_KEY` and the public `VITE_TURNSTILE_SITE_KEY`.
Keep `REPORT_CLIENT_IP_MODE=direct` locally.
