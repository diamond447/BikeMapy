#!/usr/bin/env bash
# Create a verified PostgreSQL and GPX snapshot on the production host.
set -Eeuo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
BACKUP_ID="${BACKUP_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
COMPOSE="${COMPOSE:-docker compose --env-file deploy/.env.production -f deploy/compose.production.yml}"
GPX_VOLUME="${GPX_VOLUME:-bikemapy_gpx_data}"
POSTGRES_DB="${POSTGRES_DB:-bikemapy}"
POSTGRES_USER="${POSTGRES_USER:-bikemapy}"
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
failure_marker="$BACKUP_DIR/backup-failed-${BACKUP_ID}"
services_stopped=0
restart_services() {
  if (( services_stopped )); then
    services_stopped=0
    if ! $COMPOSE up -d backend worker beat proxy; then
      echo "WARNING: failed to restart services after backup" >&2
      return 1
    fi
  fi
}
on_failure() {
  printf 'backup_id=%s\nfailed_at=%s\n' "$BACKUP_ID" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$failure_marker"
  echo "Backup failed; see $failure_marker" >&2
}
on_error() {
  on_failure
  restart_services || true
}
trap on_error ERR
trap restart_services EXIT
db_file="$BACKUP_DIR/db-${BACKUP_ID}.dump"
gpx_file="$BACKUP_DIR/gpx-${BACKUP_ID}.tar.gz"
services_stopped=1
$COMPOSE stop backend worker beat
$COMPOSE exec -T db pg_dump --username="$POSTGRES_USER" --format=custom \
  --file="/backup/db-${BACKUP_ID}.dump.part" "$POSTGRES_DB"
# The archive root is the volume root (/app/storage in production). Keep
# media/ in the archive so Django storage keys resolve after extraction.
docker run --rm -v "${GPX_VOLUME}:/data:ro" -v "$BACKUP_DIR:/backup" alpine \
  tar czf "/backup/gpx-${BACKUP_ID}.tar.gz.part" -C /data .
test -s "$BACKUP_DIR/db-${BACKUP_ID}.dump.part"
test -s "$BACKUP_DIR/gpx-${BACKUP_ID}.tar.gz.part"
mv -f "$BACKUP_DIR/db-${BACKUP_ID}.dump.part" "$db_file"
mv -f "$BACKUP_DIR/gpx-${BACKUP_ID}.tar.gz.part" "$gpx_file"
BACKUP_DIR="$BACKUP_DIR" BACKUP_ID="$BACKUP_ID" \
  bash "$(dirname "$0")/write-backup-manifest.sh"
rm -f "$failure_marker"
echo "Created verified backup $BACKUP_ID in $BACKUP_DIR"
