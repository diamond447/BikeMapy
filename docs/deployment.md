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
The backend image uses the repository root as its Docker context, with
`.dockerignore` excluding runtime configuration, backups, restore artifacts,
and local-only files. The Dockerfile then copies only `backend/` and the
runtime `scripts/` directory into the image; deployment files and operational
data are not image inputs.
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
The production Compose recovery policy is covered by a companion smoke test;
it starts the same disposable topology, kills each long-running service, and
verifies its automatic restart plus API/proxy readiness:

```sh
uv run --locked --no-dev python scripts/production_compose_smoke.py
```

Run this recovery smoke after changing service commands, dependencies, or
restart policy. It is disposable evidence only; the host reboot procedure and
production host recovery remains an operator action documented in
[operations.md](operations.md). CI also runs the smoke's `--daemon-restart`
mode, which restarts a separate digest-pinned Docker-in-Docker daemon and
verifies the services return. The mode uses `--privileged`, which is not a
security boundary and can affect the host kernel/resources; run it only on a
trusted disposable CI worker or explicitly approved host. It targets the
nested daemon rather than intentionally restarting the shared daemon, but does
not claim an absolute isolation guarantee.
The listener is explicitly HTTP because Cloudflare Tunnel terminates HTTPS at
the edge; `proxy_params` passes the public HTTPS scheme to Django without a
redirect loop. Django enforces the HTTPS redirect and emits one year of HSTS
in production, using that forwarded scheme. The Cloudflare edge remains
responsible for redirecting every public HTTP request to HTTPS before it enters
the tunnel, and for preserving the forwarded scheme. Do not expose the Nginx
listener directly or remove the `X-Forwarded-Proto` setting. Access logs are
privacy-safe JSON on container stdout, and production Compose rotates Docker
logs at 10 MiB with fourteen files per service.

The Nginx boundary strips any client-supplied `X-Forwarded-For` chain before
passing requests to Django. Django accepts `CF-Connecting-IP` only when its
direct peer is the configured Nginx address, and generic API throttles use the
same policy without a fixed proxy-count setting. This keeps an untrusted
forwarded prefix from changing a throttle identity.

Production startup fails if secure session/CSRF cookies, the HTTPS redirect,
HSTS, or the trusted HTTPS proxy header are weakened. The production settings
also make both cookies `Secure`, so owner authentication cannot establish a
cookie over an insecure request. Production Compose requires an explicit
`DJANGO_DEBUG=false` value and sets its deployment mode; it refuses to render
when that value is absent, and Django refuses to start if it is true.
The backend, worker, and scheduler commands also run `migrate --check` before
starting their long-running process. This is a startup guard, not a migration
mechanism: a release must still run the migration as a one-shot command and
must not start the candidate until that command succeeds.

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

Keep the `backend` hostname in `DJANGO_ALLOWED_HOSTS`. The production Compose
readiness probe calls `http://backend:8000/health/ready/` over the internal
network; with `DJANGO_DEBUG=false`, removing that internal hostname makes the
backend unhealthy even when the public `PUBLIC_HOST` is correct.

```sh
export COMPOSE="docker compose --env-file deploy/.env.production -f deploy/compose.production.yml"
export BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)"
export BACKUP_DIR="$PWD/backup"

# Keep .env.production (and the running Compose project) pointed at the
# previous, known-good image while taking the snapshot. Do not export the
# candidate image before backup.sh: its recovery trap deliberately restarts
# the image that was serving before the backup window.
export PREVIOUS_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$($COMPOSE ps -q backend)")"
unset BIKEMAPY_BACKEND_IMAGE

# Back up before migrations. Store these files on a separate encrypted disk.
# This writes the canonical volume-relative GPX archive and its checksum
# manifest, and stops writers for the snapshot window.
BACKUP_DIR="$BACKUP_DIR" BACKUP_ID="$BACKUP_ID" COMPOSE="$COMPOSE" \
  ./deploy/backup.sh

export BIKEMAPY_BACKEND_IMAGE="ghcr.io/diamond447/bikemapy-backend@sha256:<selected-digest>"
# Pull only the selected immutable image. Stop the previous writers before
# running its schema migration so no candidate process can start early.
$COMPOSE pull backend worker beat
$COMPOSE stop backend worker beat proxy
$COMPOSE run --rm --no-deps backend uv run --locked --no-dev python backend/manage.py migrate --noinput
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
export BACKUP_DIR="$PWD/backup"
$COMPOSE pull backend worker beat

# Restore the verified database and GPX volume as one consistent snapshot.
# restore.sh validates the manifest, preserves the current state, extracts the
# volume-relative archive under /app/storage, and leaves writers stopped if a
# recovery step fails.
BACKUP_DIR="$BACKUP_DIR" BACKUP_ID="$BACKUP_ID" COMPOSE="$COMPOSE" \
  ./deploy/restore.sh "$BACKUP_ID"

# restore.sh starts the selected previous image after a successful restore;
# validate both readiness and data reads before reopening traffic.
curl --fail --silent --show-error https://api.example.invalid/health/ready/
curl --fail --silent --show-error https://api.example.invalid/api/v1/routes/?page_size=1
```

Do not delete the current volumes until the restore has been verified. A
rollback is always a human decision; no workflow performs it automatically.
