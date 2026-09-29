#!/usr/bin/env bash
# Expire abandoned rollback volumes containing private activity upload bytes.
set -Eeuo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
LOCK_FILE="${ACTIVITY_UPLOAD_ROLLBACK_LOCK_FILE:-/var/lock/bikemapy-restore-upload-rollback.lock}"
ROLLBACK_VOLUME_PREFIX="${ACTIVITY_UPLOAD_ROLLBACK_PREFIX:-bikemapy-restore-upload-}"
CREATED_LABEL="com.bikemapy.restore-upload-rollback.created-at"
RETENTION_SECONDS="${ACTIVITY_UPLOAD_ROLLBACK_TTL_SECONDS:-86400}"

if [[ ! "$RETENTION_SECONDS" =~ ^[0-9]+$ ]] || (( RETENTION_SECONDS < 60 )); then
  echo "ACTIVITY_UPLOAD_ROLLBACK_TTL_SECONDS must be an integer of at least 60" >&2
  exit 2
fi

if [[ "${1:-}" != "--lock-held" ]]; then
  exec 9>"$LOCK_FILE"
  flock -x 9
elif [[ ! -e "$LOCK_FILE" ]]; then
  echo "Rollback reconciliation requires the restore lock file" >&2
  exit 2
fi

now="$(date +%s)"
cutoff=$((now - RETENTION_SECONDS))
failed=0
if ! volume_list="$(docker volume ls --quiet --filter "name=$ROLLBACK_VOLUME_PREFIX")"; then
  echo "WARNING: could not list private-upload rollback volumes; retry reconciliation on the next backup or restore" >&2
  exit 1
fi
while IFS= read -r volume; do
  [[ -n "$volume" ]] || continue
  [[ "$volume" == "$ROLLBACK_VOLUME_PREFIX"* ]] || continue

  created_at="$(docker volume inspect --format "{{ index .Labels \"com.bikemapy.restore-upload-rollback.created-at\" }}" "$volume" 2>/dev/null || true)"
  if [[ ! "$created_at" =~ ^[0-9]+$ ]]; then
    # Volumes created before labeled snapshots used the same dedicated prefix.
    docker_created_at="$(docker volume inspect --format '{{.CreatedAt}}' "$volume" 2>/dev/null || true)"
    created_at="$(date -d "$docker_created_at" +%s 2>/dev/null || true)"
  fi
  if [[ ! "$created_at" =~ ^[0-9]+$ ]]; then
    echo "WARNING: cannot determine age of rollback volume $volume; leaving it for a later retry" >&2
    failed=1
    continue
  fi
  if (( created_at > cutoff )); then
    continue
  fi

  echo "Expiring abandoned private-upload rollback volume $volume (age $((now - created_at))s)"
  if ! docker volume rm "$volume" >/dev/null; then
    echo "WARNING: could not remove expired private-upload rollback volume $volume; it will be retried" >&2
    failed=1
  fi
done <<< "$volume_list"

exit "$failed"
