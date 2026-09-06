#!/usr/bin/env bash
# Restore one snapshot to an explicitly selected Compose environment.
set -Eeuo pipefail
: "${1:?Usage: restore.sh BACKUP_ID}"
BACKUP_ID="$1"
BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
COMPOSE="${COMPOSE:-docker compose --env-file deploy/.env.production -f deploy/compose.production.yml}"
GPX_VOLUME="${GPX_VOLUME:-bikemapy_gpx_data}"
POSTGRES_DB="${POSTGRES_DB:-bikemapy}"
POSTGRES_USER="${POSTGRES_USER:-bikemapy}"
RESTORE_ATTEMPT_ID="${RESTORE_ATTEMPT_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
db_file="$BACKUP_DIR/db-${BACKUP_ID}.dump"
gpx_file="$BACKUP_DIR/gpx-${BACKUP_ID}.tar.gz"
manifest="$BACKUP_DIR/manifest-${BACKUP_ID}.json"
test -s "$db_file" && test -s "$gpx_file" && test -s "$manifest"
db_sum="$(jq -r .database_sha256 "$manifest")"
gpx_sum="$(jq -r .gpx_sha256 "$manifest")"
test "$db_sum" = "$(sha256sum "$db_file" | awk '{print $1}')"
test "$gpx_sum" = "$(sha256sum "$gpx_file" | awk '{print $1}')"

echo "Stopping writers before restoring backup $BACKUP_ID"
$COMPOSE stop backend worker beat
before_file="$BACKUP_DIR/gpx-before-restore-${BACKUP_ID}-${RESTORE_ATTEMPT_ID}.tar.gz"
before_db_file="$BACKUP_DIR/db-before-restore-${BACKUP_ID}-${RESTORE_ATTEMPT_ID}.dump"
before_file_name="$(basename "$before_file")"
before_db_file_name="$(basename "$before_db_file")"
rollback_db_ready=0
rollback_gpx_ready=0
rm -f "$BACKUP_DIR"/db-before-restore-${BACKUP_ID}-*.dump.part \
  "$BACKUP_DIR"/gpx-before-restore-${BACKUP_ID}-*.tar.gz.part
restore_previous_gpx() {
  if (( rollback_gpx_ready )) && [[ -s "$before_file" ]]; then
    docker run --rm -v "${GPX_VOLUME}:/data" -v "$BACKUP_DIR:/backup:ro" alpine \
      sh -c "find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf /backup/$before_file_name -C /data" \
      || echo "WARNING: automatic GPX rollback failed; restore $before_file manually" >&2
  fi
}
restore_previous_database() {
  if (( rollback_db_ready )) && [[ -s "$before_db_file" ]]; then
    $COMPOSE start db redis >/dev/null 2>&1 || true
    $COMPOSE exec -T db pg_restore --username="$POSTGRES_USER" --clean --if-exists \
      --no-owner --dbname="$POSTGRES_DB" "/backup/$before_db_file_name" \
      || echo "WARNING: automatic database rollback failed; restore $before_db_file manually" >&2
  fi
}
restore_previous_state() {
  $COMPOSE stop backend worker beat >/dev/null 2>&1 || true
  restore_previous_database
  restore_previous_gpx
  echo "Restore failed; production writers remain stopped and paired pre-restore backups are available" >&2
}
trap restore_previous_state ERR
$COMPOSE start db redis
$COMPOSE exec -T db pg_dump --username="$POSTGRES_USER" --format=custom \
  --file="/backup/$before_db_file_name.part" "$POSTGRES_DB"
test -s "$BACKUP_DIR/$before_db_file_name.part"
mv -f "$BACKUP_DIR/$before_db_file_name.part" "$before_db_file"
rollback_db_ready=1
docker run --rm -v "${GPX_VOLUME}:/data:ro" -v "$BACKUP_DIR:/backup" alpine \
  tar czf "/backup/$before_file_name.part" -C /data .
test -s "$BACKUP_DIR/$before_file_name.part"
mv -f "$BACKUP_DIR/$before_file_name.part" "$before_file"
rollback_gpx_ready=1
$COMPOSE exec -T db pg_restore --username="$POSTGRES_USER" --clean --if-exists \
  --no-owner --dbname="$POSTGRES_DB" "/backup/db-${BACKUP_ID}.dump"
docker run --rm -v "${GPX_VOLUME}:/data" -v "$BACKUP_DIR:/backup:ro" alpine \
  sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf "/backup/gpx-'"$BACKUP_ID"'.tar.gz" -C /data'
test -s "$before_file"
test -s "$before_db_file"
$COMPOSE up -d backend worker beat proxy
trap - ERR
echo "Restore completed; pre-restore GPX archive: $before_file; verify /health/ready/ and representative route reads before reopening traffic"
