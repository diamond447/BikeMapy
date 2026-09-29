#!/usr/bin/env bash
# Restore one snapshot to an explicitly selected Compose environment.
set -Eeuo pipefail
: "${1:?Usage: restore.sh BACKUP_ID}"
BACKUP_ID="$1"
BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
test -d "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
COMPOSE="${COMPOSE:-docker compose --env-file deploy/.env.production -f deploy/compose.production.yml}"
GPX_VOLUME="${GPX_VOLUME:-bikemapy_gpx_data}"
ACTIVITY_UPLOAD_VOLUME="${ACTIVITY_UPLOAD_VOLUME:-bikemapy_activity_upload_data}"
POSTGRES_DB="${POSTGRES_DB:-bikemapy}"
POSTGRES_USER="${POSTGRES_USER:-bikemapy}"
RESTORE_ATTEMPT_ID="${RESTORE_ATTEMPT_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
ACTIVITY_UPLOAD_ROLLBACK_PREFIX="${ACTIVITY_UPLOAD_ROLLBACK_PREFIX:-bikemapy-restore-upload-}"
if [[ ! "$ACTIVITY_UPLOAD_ROLLBACK_PREFIX" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*-$ ]] \
  || (( ${#ACTIVITY_UPLOAD_ROLLBACK_PREFIX} > 64 )); then
  echo "ACTIVITY_UPLOAD_ROLLBACK_PREFIX must be a short Docker-safe prefix ending in a hyphen" >&2
  exit 2
fi
safe_restore_attempt_id="${RESTORE_ATTEMPT_ID//[^a-zA-Z0-9_.-]/-}"
safe_restore_attempt_id="${safe_restore_attempt_id:0:64}"
ACTIVITY_UPLOAD_ROLLBACK_VOLUME="${ACTIVITY_UPLOAD_ROLLBACK_VOLUME:-${ACTIVITY_UPLOAD_ROLLBACK_PREFIX}${safe_restore_attempt_id}}"
rollback_volume_suffix="${ACTIVITY_UPLOAD_ROLLBACK_VOLUME#"$ACTIVITY_UPLOAD_ROLLBACK_PREFIX"}"
if [[ "$rollback_volume_suffix" == "$ACTIVITY_UPLOAD_ROLLBACK_VOLUME" \
  || ! "$rollback_volume_suffix" =~ ^[a-zA-Z0-9_.-]{1,64}$ ]]; then
  echo "ACTIVITY_UPLOAD_ROLLBACK_VOLUME must use the dedicated prefix and a bounded Docker-safe suffix" >&2
  exit 2
fi
db_file="$BACKUP_DIR/db-${BACKUP_ID}.dump"
gpx_file="$BACKUP_DIR/gpx-${BACKUP_ID}.tar.gz"
manifest="$BACKUP_DIR/manifest-${BACKUP_ID}.json"
test -s "$db_file" && test -s "$gpx_file" && test -s "$manifest"
db_sum="$(jq -r .database_sha256 "$manifest")"
gpx_sum="$(jq -r .gpx_sha256 "$manifest")"
test "$db_sum" = "$(sha256sum "$db_file" | awk '{print $1}')"
test "$gpx_sum" = "$(sha256sum "$gpx_file" | awk '{print $1}')"
verify_gpx_archive() {
  # Archives are relative to the volume root and therefore contain media/;
  # extracting one under /data recreates /app/storage/media exactly.
  bash "$(dirname "$0")/validate-gpx-archive.sh" "$gpx_file"
}
verify_gpx_archive

# Serialize restore attempts and stale-snapshot cleanup. The lock protects a
# live snapshot from expiry even if a restore lasts longer than the retention.
ROLLBACK_LOCK_FILE="${ACTIVITY_UPLOAD_ROLLBACK_LOCK_FILE:-/var/lock/bikemapy-restore-upload-rollback.lock}"
exec 9>"$ROLLBACK_LOCK_FILE"
flock -x 9
ACTIVITY_UPLOAD_ROLLBACK_PREFIX="$ACTIVITY_UPLOAD_ROLLBACK_PREFIX" \
  bash "$(dirname "$0")/reconcile-restore-upload-rollbacks.sh" --lock-held

echo "Stopping writers before restoring backup $BACKUP_ID"
$COMPOSE stop backend worker beat
before_file="$BACKUP_DIR/gpx-before-restore-${BACKUP_ID}-${RESTORE_ATTEMPT_ID}.tar.gz"
before_db_file="$BACKUP_DIR/db-before-restore-${BACKUP_ID}-${RESTORE_ATTEMPT_ID}.dump"
before_file_name="$(basename "$before_file")"
before_db_file_name="$(basename "$before_db_file")"
rollback_db_ready=0
rollback_gpx_ready=0
rollback_activity_uploads_ready=0
rollback_activity_upload_volume_owned=0
preserve_activity_upload_rollback=0
cleanup_activity_upload_rollback() {
  if (( rollback_activity_upload_volume_owned && ! preserve_activity_upload_rollback )); then
    if ! docker volume rm "$ACTIVITY_UPLOAD_ROLLBACK_VOLUME" >/dev/null 2>&1; then
      echo "WARNING: private-upload rollback volume $ACTIVITY_UPLOAD_ROLLBACK_VOLUME remains and will be retried after expiration" >&2
    fi
  fi
  ACTIVITY_UPLOAD_ROLLBACK_PREFIX="$ACTIVITY_UPLOAD_ROLLBACK_PREFIX" \
    bash "$(dirname "$0")/reconcile-restore-upload-rollbacks.sh" --lock-held \
    || echo "WARNING: stale private-upload rollback volumes could not all be removed; the next backup or restore will retry" >&2
}
trap cleanup_activity_upload_rollback EXIT
rm -f "$BACKUP_DIR"/db-before-restore-${BACKUP_ID}-*.dump.part \
  "$BACKUP_DIR"/gpx-before-restore-${BACKUP_ID}-*.tar.gz.part
restore_previous_gpx() {
  if (( rollback_gpx_ready )) && [[ -s "$before_file" ]]; then
    docker run --rm -v "${GPX_VOLUME}:/data" -v "$BACKUP_DIR:/backup:ro" alpine \
      sh -c "find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf /backup/$before_file_name -C /data" \
      || echo "WARNING: automatic GPX rollback failed; restore $before_file manually" >&2
  fi
}
restore_previous_activity_uploads() {
  if (( rollback_activity_uploads_ready )); then
    if ! docker run --rm \
      -v "${ACTIVITY_UPLOAD_VOLUME}:/data" \
      -v "${ACTIVITY_UPLOAD_ROLLBACK_VOLUME}:/rollback:ro" alpine \
      sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf /rollback/activity-uploads.tar.gz -C /data'; then
      echo "WARNING: transient activity uploads could not be rolled back; rollback volume $ACTIVITY_UPLOAD_ROLLBACK_VOLUME was preserved" >&2
      preserve_activity_upload_rollback=1
    fi
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
  restore_previous_activity_uploads
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
  tar czf "/backup/$before_file_name.part" \
  --exclude=./media/private/activity_uploads -C /data .
test -s "$BACKUP_DIR/$before_file_name.part"
mv -f "$BACKUP_DIR/$before_file_name.part" "$before_file"
rollback_gpx_ready=1
if docker volume inspect "$ACTIVITY_UPLOAD_ROLLBACK_VOLUME" >/dev/null 2>&1; then
  echo "Rollback volume $ACTIVITY_UPLOAD_ROLLBACK_VOLUME already exists; refusing to reuse it" >&2
  exit 1
fi
docker volume create \
  --label com.bikemapy.restore-upload-rollback=true \
  --label "com.bikemapy.restore-upload-rollback.created-at=$(date +%s)" \
  "$ACTIVITY_UPLOAD_ROLLBACK_VOLUME" >/dev/null
rollback_activity_upload_volume_owned=1
docker run --rm \
  -v "${ACTIVITY_UPLOAD_VOLUME}:/source:ro" \
  -v "${ACTIVITY_UPLOAD_ROLLBACK_VOLUME}:/rollback" alpine \
  sh -c 'tar czf /rollback/activity-uploads.tar.gz -C /source .'
docker run --rm -v "${ACTIVITY_UPLOAD_ROLLBACK_VOLUME}:/rollback:ro" alpine \
  test -s /rollback/activity-uploads.tar.gz
rollback_activity_uploads_ready=1
$COMPOSE exec -T db pg_restore --username="$POSTGRES_USER" --clean --if-exists \
  --no-owner --dbname="$POSTGRES_DB" "/backup/db-${BACKUP_ID}.dump"
docker run --rm -v "${GPX_VOLUME}:/data" -v "$BACKUP_DIR:/backup:ro" alpine \
  sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf "/backup/gpx-'"$BACKUP_ID"'.tar.gz" --exclude=./media/private/activity_uploads -C /data'
echo "Migrating restored schema, then discarding transient upload references"
$COMPOSE run --rm --no-deps backend uv run --locked --no-dev \
  python backend/manage.py migrate --noinput
$COMPOSE run --rm --no-deps backend uv run --locked --no-dev \
  python backend/manage.py discard_restored_activity_uploads
docker run --rm -v "${ACTIVITY_UPLOAD_VOLUME}:/data" alpine \
  sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} +'
test -s "$before_file"
test -s "$before_db_file"
$COMPOSE up -d backend worker beat proxy
trap - ERR
echo "Restore completed; pre-restore GPX archive: $before_file; verify /health/ready/ and representative route reads before reopening traffic"
