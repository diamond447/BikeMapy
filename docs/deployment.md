# Deployment and operations

No workflow deploys production. The operator picks an image, deploys it on the
host and promotes the frontend by hand.

## Backend image

After `Quality checks` passes for a push to `main`, the `Backend image`
workflow publishes the commit it tested:

```text
ghcr.io/diamond447/bikemapy-backend:sha-<full-commit-sha>
```

Tags are never overwritten; deploy the digest recorded in the workflow's
`manifest.json` (`image@sha256:…`), never a floating tag. The image holds the
locked environment, `backend/`, `scripts/` and the collected static files.
It contains no uv and no GDAL tools, so commands inside it call `python`,
`gunicorn` and `celery` directly.

## Frontend (Cloudflare Pages)

| Setting | Value |
| --- | --- |
| Root directory | `frontend` |
| Build command | `pnpm install --frozen-lockfile && pnpm build:pages` |
| Output directory | `dist` |
| `VITE_API_URL` | public API origin (HTTPS) |
| `VITE_PUBLIC_SITE_URL` | canonical site URL |
| `VITE_ENABLE_REPORTS` | `true` only when reporting is enabled |
| `VITE_TURNSTILE_SITE_KEY` | public Turnstile key, if reports are enabled |
| `VITE_CF_WEB_ANALYTICS_TOKEN` | optional |

`build:pages` builds the production variables only on `main`; every other
branch builds a read-only preview without the report form. Never put a server
secret in a `VITE_*` variable. Keep automatic production deployment off and
promote a reviewed build from the Pages dashboard.

The Pages Functions in `frontend/functions/` add route metadata to shared
route URLs and serve the sitemap. They need `PUBLIC_API_ORIGIN` and
`PUBLIC_SITE_URL` (or the `VITE_*` equivalents).

## Host setup

The API runs on one host behind Cloudflare Tunnel: `cloudflared` forwards the
API hostname to Nginx on `127.0.0.1:8080`, Nginx proxies to Django, and
Django enforces HTTPS, HSTS and secure cookies using the forwarded scheme.
Do not expose the Nginx listener directly.

Create `deploy/.env.production` from `.env.example` (`chmod 600`) and
replace every placeholder. Production refuses to start
with a weak or default `DJANGO_SECRET_KEY`, with `DJANGO_DEBUG` unset or
true, or with weakened cookie and HTTPS settings. Keep `backend` in
`DJANGO_ALLOWED_HOSTS`; the internal health check uses it. For client IPs
behind the tunnel, set `REPORT_CLIENT_IP_MODE=cloudflare` and trust only the
Nginx address: `REPORT_TRUSTED_PROXY_CIDRS=172.30.0.2/32`.

The production Compose file needs Docker Compose 2.17 or newer. All services
use `restart: unless-stopped`; the backend, worker and beat run under
Docker's init and check `migrate --check` before starting.

## Release

```sh
export COMPOSE=./deploy/compose.sh
export BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)" BACKUP_DIR="$PWD/backup"

# Back up while the previous image is still selected.
unset BIKEMAPY_BACKEND_IMAGE
BACKUP_DIR="$BACKUP_DIR" BACKUP_ID="$BACKUP_ID" COMPOSE="$COMPOSE" ./deploy/backup.sh

export BIKEMAPY_BACKEND_IMAGE="ghcr.io/diamond447/bikemapy-backend@sha256:<digest>"
$COMPOSE pull backend worker beat
$COMPOSE stop backend worker beat proxy
$COMPOSE run --rm --no-deps backend python backend/manage.py migrate --noinput
$COMPOSE up -d backend worker beat proxy
curl --fail https://api.example.invalid/health/ready/
```

Record the digest, backup ID and migration output. Run migrations and other
maintenance commands with `run --rm --no-deps`, never with `up`.

## Rollback

Releases are expand/contract: a release that removes code keeps its tables,
so the previous image still starts against the migrated database. For an
application failure, select the previous digest and run
`$COMPOSE pull backend worker beat && $COMPOSE up -d backend worker beat proxy`.
If data must be restored, use `deploy/restore.sh` (below) with the previous
image selected. CI rehearses the image rollback on every pull request.

## Lean mode

A home server that only serves the catalogue can run without Redis and Celery.
Set `BIKEMAPY_LEAN=true` in `deploy/.env.production`; `deploy/compose.sh`, which
the commands above and the backup scripts use, then adds
`deploy/compose.lean.yml` (Docker Compose 2.24.4 or newer). Only `db`,
`backend` and `proxy` start, and the admin index shows the active mode.

In lean mode tasks run inline in the request that triggers them, and the
cache used for throttles, report limits and viewport epochs is the
`bikemapy_cache` table in PostgreSQL. Its increments are not atomic, so
simultaneous reports can slightly exceed the report limit. There is no beat:
install [`maintenance.cron.example`](../deploy/maintenance.cron.example), which
runs `manage.py run_periodic_tasks` hourly and with `--with-crawl` once a day.
Use the full topology for heavy ingestion, because a crawl then runs inside
the cron process.

Switching a running host to lean mode: stop and remove `worker`, `beat` and
`redis` first, since Compose leaves running containers of services that became
profile-gated, then set the flag and run `$COMPOSE up -d`. In the release and
rollback commands, list `backend proxy` instead of `backend worker beat proxy`.
To go back, remove the flag and run `$COMPOSE up -d`.

## Health checks

| Endpoint | Meaning |
| --- | --- |
| `/health/live/` | The web process answers. |
| `/health/ready/` | PostgreSQL and the cache (Redis, or PostgreSQL in lean mode) respond. |
| `/health/crawler/` | The incremental crawl succeeded within `CRAWLER_FRESHNESS_MAX_AGE` (36 h). |

Check them from outside the host and alert after two consecutive failures.
If a service stays unhealthy, inspect `$COMPOSE ps` and `$COMPOSE logs`, then
run `$COMPOSE up -d` and the readiness check again.

## Backups and restore

`deploy/backup.sh`, run daily, stops the writers briefly and writes a
PostgreSQL dump, a GPX volume archive and a SHA-256 manifest to `BACKUP_DIR`.
Run `deploy/check-backup-freshness.sh` hourly to catch a silent failure, and
`deploy/retention.sh` to keep the newest 30 snapshots on the host.

`deploy/pull-backups.sh` copies the snapshots to a laptop over a read-only
SSH account and encrypts them with `age`; the laptop keeps the newest 90. The
account and forced-command examples are in `deploy/*.example`.

`deploy/restore.sh BACKUP_ID` verifies the manifest, saves the current
database and GPX volume, restores both and starts the services; if a step
fails it rolls back and leaves the writers stopped. Verify `/health/ready/`
and a route read before reopening traffic. `deploy/restore-drill.sh BACKUP_ID`
restores a snapshot into disposable containers without touching production;
CI runs it every week.

## Logs and errors

Django and Celery log JSON lines to stdout with secrets and IP addresses
redacted; Docker rotates them at 10 MiB (14 files in production). Sentry is
optional (`SENTRY_DSN`); events are stripped of request, user and local
variable data, and the project retention must be set to 30 days.

## Upgrading a host that ran the game

`accounts.0030` drops the retired game tables. After it has run:

1. Run `manage.py remove_stale_contenttypes --include-stale-apps`.
2. Delete former player users (everyone except the owner listed in
   `GITHUB_OWNER_IDS`).
3. Remove the `activity_upload_data` volume, any `bikemapy-restore-upload-*`
   volumes and `media/private/activity_uploads` in the GPX volume.

Purge leftover game tasks from Redis with `celery -A config purge` if the
worker logs unknown `bikemapy.accounts.*` messages.
