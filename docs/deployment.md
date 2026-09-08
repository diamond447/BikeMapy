# Deployment and release runbook

This project deliberately separates build artifacts, runtime configuration, and
production deployment. No workflow in this repository deploys the production
backend or merges a pull request.

Operational health checks, daily snapshots, encrypted laptop pulls, restore
drills, Sentry privacy controls, and their honest recovery limitations are
documented in [operations.md](operations.md).

## Backend image

The `Backend image` workflow builds the tested `docker/backend.Dockerfile` on
pull requests and on pushes to `main`. Only a push to `main` or an explicitly
started `workflow_dispatch` can publish to GitHub Container Registry. The
published reference is commit-addressable:

```text
ghcr.io/diamond447/bikemapy-backend:sha-<full-commit-sha>
```

The workflow also uploads `manifest.json`, which records the image digest. The
workflow refuses to overwrite an existing `sha-<commit>` tag, so a rerun keeps
the original artifact intact. Use the recorded digest (`image@sha256:…`) for
deployment; do not deploy a floating tag such as `latest`. The provenance
artifact and OCI revision label make the selected build auditable.

The image contains application code, locked runtime dependencies, and the
collected Django static assets under `/app/staticfiles`. In production,
WhiteNoise serves that immutable asset set through the Nginx `/static/` proxy;
the development Compose stack leaves static serving to Django's runserver.
Secrets, database data, GPX files, OAuth credentials, and deployment settings
remain in the host environment or Docker volumes. Keep `.env.production` on
the host and restrict its permissions (`chmod 600`). The production Compose
file passes the full application environment only to Django/Celery services;
PostgreSQL receives only its database variables, Redis receives none, and the
Nginx proxy receives no secrets.

## Cloudflare Pages

Create one Pages project connected to this repository with these settings:

| Setting | Production | Pull-request preview |
| --- | --- | --- |
| Root directory | `frontend` | `frontend` |
| Build command | `pnpm install --frozen-lockfile && pnpm build:pages` | same command |
| Output directory | `dist` | `dist` |
| `VITE_API_URL` | production read API origin | the same production read API origin |
| `VITE_PUBLIC_SITE_URL` | canonical production URL | the preview URL or Pages project URL |
| `VITE_CF_WEB_ANALYTICS_TOKEN` | public Cloudflare Web Analytics token | empty unless the owner has reviewed the property |
| `VITE_ENABLE_REPORTS` | `true` only when reporting is approved | `false` |
| `VITE_TURNSTILE_SITE_KEY` | public site key, if reporting is enabled | empty |

Preview builds therefore query production catalogue data but do not render the
report action or include its mutation path after Vite's production tree-shake.
The frontend has no admin UI; owner administration stays on the protected
backend origin. Never add a `DJANGO_*`, database, OAuth secret, Turnstile
secret, or signing key as a `VITE_*` variable. Run the bundle scan after every
production build:

```sh
python scripts/check_frontend_bundle.py frontend/dist
```

`build:pages` uses Cloudflare's `CF_PAGES_BRANCH`: the production branch
(`main`) uses the configured production variables, while every other branch
forces `VITE_ENABLE_REPORTS=false` and runs the read-only scan. This keeps one
Cloudflare-supported build command for both production and preview builds.

Configure the Pages project to build pull requests for review. Keep automatic
production deployment disabled: after a reviewed change, a human may promote
the tested production build from the Pages dashboard. This keeps production
release approval separate from GitHub Actions and from pull-request previews.

## Reverse proxy and Cloudflare Tunnel

`deploy/nginx.conf` proxies health, API, account, owner-admin, and static asset
paths to the backend. The GPX endpoint remains authorized by Django and uses
an `X-Accel-Redirect` internal handoff to stream directly from the read-only
GPX volume; `/storage/` and `/media/` never map directly to a public location.
Static files are collected during the image build and served by WhiteNoise
only when `DEBUG=False`.

The production asset path is covered by a disposable-stack smoke test. Run it
from the repository root before releasing an image:

```sh
uv run --locked --no-dev python scripts/check_production_static_assets.py
```

It builds the current backend image, starts the production Compose topology,
and verifies HTTP 200 responses for the Admin stylesheet and navigation
script through Nginx.
The listener is explicitly HTTP because Cloudflare Tunnel terminates HTTPS at
the edge; `proxy_params` passes the public HTTPS scheme to Django without a
redirect loop. Access logs are privacy-safe JSON on container stdout, and
production Compose rotates Docker logs at 10 MiB with fourteen files per
service.

The repository does not assume a homeserver exists. When one is available,
install `cloudflared` on that host and route a named tunnel to the local Nginx
listener. The tunnel is outbound-only; no router port-forward is needed:

```sh
cloudflared tunnel login
cloudflared tunnel create bikemapy-api
cloudflared tunnel route dns bikemapy-api api.example.invalid
```

Save the generated tunnel UUID and credentials path in the host-only
`~/.cloudflared/config.yml` (never commit the credentials file):

```yaml
tunnel: <tunnel-uuid>
credentials-file: /home/bikemapy/.cloudflared/<tunnel-uuid>.json
ingress:
  - hostname: api.example.invalid
    service: http://127.0.0.1:8080
  - service: http_status:404
```

Then verify the route and run the named tunnel as a service:

```sh
cloudflared tunnel ingress validate
cloudflared tunnel run bikemapy-api
```

For a permanent service, use the distribution's `cloudflared service install`
and a least-privilege tunnel credentials file. Restrict the tunnel ingress to
the API hostname and set `PUBLIC_HOST` in `.env.production`. Before using
`REPORT_CLIENT_IP_MODE=cloudflare`, trust only the direct Nginx peer
`172.30.0.2/32` in `REPORT_TRUSTED_PROXY_CIDRS`; do not list public Cloudflare
CIDRs because Django sees Nginx as `REMOTE_ADDR`. Never trust arbitrary
forwarded headers.
The production Compose network gives Nginx the fixed address `172.30.0.2`;
the example trusts only `172.30.0.2/32`, and Nginx forwards the Cloudflare
`CF-Connecting-IP` header after the tunnel has established the boundary.

## Manual backend deployment

The operator must choose the exact image before each release. From the host,
after copying `deploy/.env.production.example` to `deploy/.env.production` and
editing every placeholder:

```sh
export COMPOSE="docker compose --env-file deploy/.env.production -f deploy/compose.production.yml"
export BIKEMAPY_BACKEND_IMAGE="ghcr.io/diamond447/bikemapy-backend@sha256:<selected-digest>"
export BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)"
export POSTGRES_USER="${POSTGRES_USER:-bikemapy}"
mkdir -p backup

# Back up before migrations. Store these files on a separate encrypted disk.
$COMPOSE exec -T db pg_dump --username="$POSTGRES_USER" --format=custom --file="/backup/db-${BACKUP_ID}.dump" bikemapy
docker run --rm -v bikemapy_gpx_data:/data:ro -v "$PWD/backup:/backup" alpine \
  tar czf "/backup/gpx-${BACKUP_ID}.tar.gz" -C / data

# Pull only the selected immutable image, then migrate that image.
$COMPOSE pull backend worker beat
$COMPOSE run --rm backend uv run --locked --no-dev python backend/manage.py migrate --noinput
$COMPOSE up -d backend worker beat proxy

# Do not continue until readiness and the public API respond successfully.
curl --fail --silent --show-error https://api.example.invalid/health/ready/
curl --fail --silent --show-error https://api.example.invalid/api/v1/
```

Record the selected digest, backup ID, migration output, and readiness response
in the release log. A backup is not a monitoring system: verify that the files
exist and periodically test restoring them on an isolated stack.

## Rollback

Keep the previous image digest and backup ID with every release. For an
application-only failure, select the previous digest and restart without
running new migrations:

```sh
export COMPOSE="docker compose --env-file deploy/.env.production -f deploy/compose.production.yml"
export BIKEMAPY_BACKEND_IMAGE="ghcr.io/diamond447/bikemapy-backend@sha256:<previous-digest>"
$COMPOSE pull backend worker beat
$COMPOSE up -d backend worker beat proxy
curl --fail --silent --show-error https://api.example.invalid/health/ready/
```

If the migration is incompatible or data must be restored, stop writers first,
restore the database backup while the production database is in maintenance,
restore the GPX volume,
then start the previous image and verify readiness and representative route
reads before reopening traffic. The following explicit sequence keeps writers
stopped and validates the restored database before traffic is reopened:

```sh
# Select and pull the previous image before maintenance or data restoration.
export COMPOSE="docker compose --env-file deploy/.env.production -f deploy/compose.production.yml"
export BIKEMAPY_BACKEND_IMAGE="ghcr.io/diamond447/bikemapy-backend@sha256:<previous-digest>"
export BACKUP_ID="<selected-backup-id>"
$COMPOSE pull backend worker beat

# Maintenance mode: stop API workers and prevent new writes while restoring.
$COMPOSE stop backend worker beat

# Restore to the production database only after checking the backup exists.
test -s "backup/db-${BACKUP_ID}.dump"
$COMPOSE start db redis
$COMPOSE exec -T db pg_restore --username="$POSTGRES_USER" --clean --if-exists --no-owner --dbname=bikemapy \
  "/backup/db-${BACKUP_ID}.dump"

# Preserve the current GPX volume before replacing its contents.
test -s "backup/gpx-${BACKUP_ID}.tar.gz"
docker run --rm -e BACKUP_ID="$BACKUP_ID" -v bikemapy_gpx_data:/data:ro \
  -v "$PWD/backup:/backup" alpine \
  sh -c 'tar czf "/backup/gpx-before-restore-${BACKUP_ID}.tar.gz" -C / data'
docker run --rm -e BACKUP_ID="$BACKUP_ID" -v "$PWD/backup:/backup" alpine \
  sh -c 'tar -tzf "/backup/gpx-${BACKUP_ID}.tar.gz" >/dev/null'
docker run --rm -v bikemapy_gpx_data:/data alpine sh -c 'rm -rf /data/*'
docker run --rm -e BACKUP_ID="$BACKUP_ID" -v bikemapy_gpx_data:/data \
  -v "$PWD/backup:/backup" alpine \
  sh -c 'tar xzf "/backup/gpx-${BACKUP_ID}.tar.gz" -C / && test -n "$(find /data -type f -print -quit)"'
test -s "backup/gpx-before-restore-${BACKUP_ID}.tar.gz"

# Start the selected previous image and validate both readiness and data reads.
$COMPOSE up -d backend worker beat proxy
curl --fail --silent --show-error https://api.example.invalid/health/ready/
curl --fail --silent --show-error https://api.example.invalid/api/v1/routes/?page_size=1
```

Do not delete the current volumes until the restore has been verified. A
rollback is always a human decision; no workflow performs it automatically.
