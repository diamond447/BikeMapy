# Operations, monitoring, and recovery

This runbook describes the lightweight production operations model. It is an
operator procedure, not an availability or disaster-recovery guarantee.

## Health and external checks

Use three independent HTTPS checks from an external uptime provider:

| URL | Meaning | Alert |
| --- | --- | --- |
| `/health/live/` | The web process can answer without checking dependencies. | Any non-2xx response. |
| `/health/ready/` | PostgreSQL and the configured Redis cache complete a round trip. | Any non-2xx response; this is the traffic-readiness check. |
| `/health/crawler/` | The incremental crawler checkpoint succeeded within `CRAWLER_FRESHNESS_MAX_AGE` (36 hours by default). | Any non-2xx response; this indicates stale ingestion, not API downtime. |

Configure checks from outside the host and notify the operator on two
consecutive failures. The endpoint responses intentionally contain status and
timestamps only; they do not expose report content or client addresses.

## Service restart and host recovery

The production Compose file applies `restart: unless-stopped` to every
long-running service: `db`, `redis`, `backend`, `worker`, `beat`, and `proxy`.
Docker therefore recreates a stopped process after an unexpected exit and
starts existing containers when the Docker daemon starts after a host reboot.
This is a recovery policy, not a process supervisor or a high-availability
guarantee. A container deliberately stopped by an operator stays stopped until
it is explicitly started again.

The production file requires Docker Compose 2.17.0 or newer because it uses
the long-form `depends_on.restart` option. Verify the host before deployment:

```sh
docker compose version
```

Compose waits for healthy PostgreSQL and Redis before creating the backend,
worker, and scheduler. The proxy waits for a healthy backend. Dependency
entries also use `restart: true`, so an intentional `docker compose restart`
or a Compose update/recreate of a dependency propagates to its dependents. An
unchanged `docker compose up -d dependency` is a no-op and does not propagate a
restart. An unexpected dependency crash is handled by Docker restarting that dependency;
the application processes reconnect where supported, and the operator should
reconcile the full stack if a dependent remains unhealthy:

```sh
export COMPOSE="docker compose --env-file deploy/.env.production -f deploy/compose.production.yml"
$COMPOSE ps
$COMPOSE logs --since=10m db redis backend worker beat proxy
$COMPOSE up -d db redis backend worker beat proxy
$COMPOSE ps
curl --fail --silent --show-error https://api.example.invalid/health/ready/
```

After a host reboot, first ensure Docker is enabled and running, then run the
same `up -d` reconciliation and readiness check. For an unexpected service
exit, inspect `ps`, the service logs, and the container exit/restart count
before deciding whether to leave the automatic restart in place or stop the
affected service for investigation. Verify `/health/live/`, `/health/ready/`,
and `/health/crawler/` as applicable; a healthy API does not prove that the
crawler is current.

Migrations and other maintenance commands are one-shot jobs. Run them with
`docker compose run --rm backend ...` (as in the deployment runbook), never
with `docker compose up`; the command must finish or fail visibly and must not
be retried indefinitely by a restart policy. The backup and restore scripts
stop writers during their maintenance window and explicitly start the
long-running services again.

The disposable production Compose smoke kills each of the six services and
asserts that Docker reports a running container with an increased restart
count, then checks backend and proxy readiness. Run it after changing the
Compose contract:

```sh
uv run --locked --no-dev python scripts/production_compose_smoke.py
```

CI additionally runs the smoke with `--daemon-restart`. That mode creates a
separate, digest-pinned Docker-in-Docker daemon and data volume, runs the
production Compose stack inside it, restarts only that disposable daemon
container, and verifies all six `unless-stopped` services and proxy readiness
after the daemon returns. The privileged container is not a security boundary:
it can access host kernel resources and may affect the host, so run this mode
only on a trusted disposable CI worker or an explicitly approved local host.
The harness targets the nested daemon and does not issue a restart to the
developer/CI host's shared daemon, but this isolation must not be treated as a
security guarantee. To run the stronger rehearsal locally, use a Docker
installation that allows privileged containers:

```sh
uv run --locked --no-dev python scripts/production_compose_smoke.py --daemon-restart
```

## Logs and Sentry

Django and Celery write one JSON object per line to stdout. The formatter
redacts secret-like values and IP addresses, and Nginx access logs deliberately
omit the remote address. Docker rotates each service's JSON log files (10 MiB
per file, five files locally, and fourteen in production). This is bounded
diagnostic storage, not an audit log.

Set `SENTRY_DSN` only in the host-only production environment. The integration
uses `send_default_pii=false`, disables local-variable capture, and a
`before_send` scrubber with explicit allow-lists. It drops request and user
payloads, exception values and frame locals/source context, breadcrumb
messages/data, custom contexts, and arbitrary extras. It retains only
typed event IDs/timestamps, finite levels, fixed platform metadata, exception
type/mechanism and line/column markers,
validated breadcrumb timestamps and finite type/category/level values, and
structurally validated trace IDs and span IDs. Transaction events use the same
boundary and drop their names and spans. The scrubber is covered by the
ordinary-discovery regression
in `backend/tests/test_sentry_privacy.py` (with lower-level cases in
`backend/config/tests/test_observability.py`). Set the Sentry project retention
to exactly 30 days; settings reject any other `SENTRY_RETENTION_DAYS` value,
but the SDK cannot enforce account-level retention. Record dashboard or Sentry
API evidence of the 30-day project setting during deployment; the application
setting and test alone are not proof of the hosted retention policy.

Celery Beat reconciles orphaned GPX payload cleanup every 15 minutes. Cleanup
claims are leased, retries use bounded exponential backoff, and records become
`exhausted` after five attempts by default
(`GPX_ORPHAN_CLEANUP_MAX_ATTEMPTS`). Exhausted records remain visible in the
owner admin for manual investigation; retry delay and lease duration are
configurable with the `GPX_ORPHAN_CLEANUP_*` settings.
New GPX payload keys retain the checksum for diagnostics and add the immutable
extraction-attempt ID, preventing stale cleanup from colliding with a later
import; existing stored keys remain readable and removable.

## Daily snapshots

Create a restricted backup directory owned by the deployment operator and run
`deploy/backup.sh` once per day (for example, from a systemd timer). It writes
a PostgreSQL custom dump, a GPX volume tarball, a SHA-256 manifest, and a
failure marker. The GPX archive is rooted at the Docker volume root
(`/app/storage` in production), so Django's `media/gpx/...` storage paths are
preserved inside the archive. `deploy/restore.sh` extracts the archive back at
that same volume root; it never extracts at `/` or creates a nested `data/`
directory. The script exits non-zero if either artifact is missing or empty.
Alert on the exit status and run
`BACKUP_DIR=/srv/bikemapy/backup deploy/check-backup-freshness.sh` at least
hourly; this detects a silent scheduler or disk failure.

The backup script stops the API and Celery writers for the bounded snapshot
window so the PostgreSQL and GPX artifacts describe one consistent point in
time, then restarts them through an exit trap even when the snapshot fails.
Schedule it during a low-traffic maintenance window and expect a brief read
outage; it does not provide zero-downtime backups.

The host needs Docker Compose, `sha256sum`, `jq`, and the Alpine image used for
the GPX archive step. The restore drill additionally needs the PostGIS image.

The production Compose database mounts this directory at `/backup`. Keep the
directory outside Git and never place `.env.production` or credentials in it.

Archives created by older copies of the deployment runbook may contain a
top-level `data/` directory. They are not compatible with the canonical
restore command because that would produce `/app/storage/data/...`; do not
restore one as-is. On an isolated host, unpack the old archive, repack its
`data/` contents with `tar czf ... -C <unpacked-data> .`, and generate a new
manifest with `BACKUP_ID=<new-id> BACKUP_DIR=<backup-dir>
bash deploy/write-backup-manifest.sh`. Backups produced by `deploy/backup.sh` and
the scheduled restore-drill workflow already use the canonical volume-relative
layout.

## Laptop copy

Create a dedicated host account (for example `bikemapy-backup`) with a valid
login shell but no interactive shell access, sudo, or write permission. Use a
read-only forced SSH command restricted to the backup directory. The compatible
`authorized_keys` and forced-command examples
are `deploy/backup-authorized-keys.example` and
`deploy/bikemapy-backup-readonly.example`. From the
laptop, configure `BACKUP_SSH_TARGET`, `BACKUP_REMOTE_DIR`,
`LAPTOP_BACKUP_DIR`, and `BACKUP_AGE_RECIPIENT`, then run
`deploy/pull-backups.sh`. It transfers only snapshots and manifests over SSH
and encrypts every file with `age` before writing it to the laptop directory.
Schedule `check-backup-freshness.sh` against the laptop copy and alert when the
newest manifest is older than 48 hours. Set `BACKUP_AGE_IDENTITY` for the age
private key when running the check so it decrypts the manifest and verifies
both encrypted artifact checksums. Without the identity, the check verifies
the timestamped complete file set only and reports that limitation. Restrict the laptop directory to
the backup operator and store it on an encrypted disk.

Run `deploy/retention.sh` after successful backups: the host keeps the newest
30 complete snapshots and the laptop keeps the newest 90 encrypted snapshots.
It always preserves the newest recovery points and removes only older files
with the same snapshot ID. The example schedules are in
`deploy/retention.cron.example`.

This laptop is a separate physical copy, but it is not cloud storage,
geographic disaster protection, high availability, or an immutable backup.
The project does not claim protection from simultaneous loss of the host and
laptop, theft, ransomware, or an unavailable upstream provider.

## Restore and evidence

Select a verified backup ID and a previously reviewed image. Before restoring
production, stop `backend`, `worker`, and `beat`, preserve the current GPX
volume, and run:

```sh
BACKUP_DIR=/srv/bikemapy/backup GPX_VOLUME=bikemapy_gpx_data POSTGRES_USER=bikemapy \
  ./deploy/restore.sh 20260101T030000Z
```

The script verifies manifest checksums, archives the current database and GPX
volume as `db-before-restore-<id>.dump` and
`gpx-before-restore-<id>.tar.gz` before changing anything, restores the
database with `pg_restore`, replaces the GPX volume, and starts the services
again. If any restore step fails, it attempts to restore both pre-restore
artifacts and leaves writers stopped for operator verification. The
operator must verify `/health/ready/`, `/health/crawler/`, and representative
route reads before reopening traffic. Keep the backup ID, image digest,
operator, timestamps, command output, and endpoint responses as recovery
evidence. Do not delete the pre-restore volume or backup until verification is
complete.

Run a non-production drill every Sunday using
`deploy/restore-drill.sh BACKUP_ID` and the supplied
`deploy/restore-drill.cron.example`. It uses a disposable PostGIS container
and validates both the database restore and GPX archive without touching
production volumes. The drill verifies the manifest checksums, restores the
database with the explicit PostgreSQL role, queries the restored route count,
and writes `restore-drill-<id>.json` evidence. Record success/failure,
duration, backup ID, and the verified row/route count in the operator log. A
drill proves only that this snapshot can be read in this environment; it does
not prove a complete production failover.

The scheduled/manual GitHub Actions workflow
`.github/workflows/restore-drill.yml` also builds a representative database and
GPX snapshot, runs the same drill in disposable containers, and uploads the
route-count evidence artifact. It is a non-production demonstration, not a
production recovery guarantee.
