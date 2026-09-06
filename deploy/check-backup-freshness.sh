#!/usr/bin/env bash
# Fail when the newest complete, verified snapshot is older than the policy.
set -Eeuo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
MAX_AGE_SECONDS="${MAX_AGE_SECONDS:-172800}"
test -d "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
latest="$(find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'manifest-*.json' -o -name 'manifest-*.json.age' \) -printf '%f\n' |
  sort | tail -n 1 || true)"
if [[ -z "$latest" ]]; then
  echo "No backup manifest found in $BACKUP_DIR" >&2
  exit 1
fi

if [[ "$latest" == manifest-*.json.age ]]; then
  backup_id="${latest#manifest-}"
  backup_id="${backup_id%.json.age}"
  for suffix in db-"$backup_id".dump.age gpx-"$backup_id".tar.gz.age manifest-"$backup_id".json.age; do
    test -s "$BACKUP_DIR/$suffix" || {
      echo "Encrypted backup set is incomplete: $suffix" >&2
      exit 1
    }
  done
  if [[ -n "${BACKUP_AGE_IDENTITY:-}" ]]; then
    decrypted_dir="$(mktemp -d)"
    cleanup_decrypted() { rm -rf "$decrypted_dir"; }
    trap cleanup_decrypted EXIT
    age -d -i "$BACKUP_AGE_IDENTITY" -o "$decrypted_dir/manifest.json" \
      "$BACKUP_DIR/manifest-${backup_id}.json.age"
    jq -e --arg id "$backup_id" '.backup_id == $id' "$decrypted_dir/manifest.json" >/dev/null
    db_name="$(jq -r .database "$decrypted_dir/manifest.json")"
    gpx_name="$(jq -r .gpx "$decrypted_dir/manifest.json")"
    [[ "$db_name" == "db-${backup_id}.dump" && "$gpx_name" == "gpx-${backup_id}.tar.gz" ]]
    age -d -i "$BACKUP_AGE_IDENTITY" -o "$decrypted_dir/db.dump" "$BACKUP_DIR/$db_name.age"
    age -d -i "$BACKUP_AGE_IDENTITY" -o "$decrypted_dir/gpx.tar.gz" "$BACKUP_DIR/$gpx_name.age"
    printf '%s  %s\n%s  %s\n' "$(jq -r .database_sha256 "$decrypted_dir/manifest.json")" \
      "$decrypted_dir/db.dump" "$(jq -r .gpx_sha256 "$decrypted_dir/manifest.json")" \
      "$decrypted_dir/gpx.tar.gz" | sha256sum -c - >/dev/null
    echo "Encrypted backup is fresh and checksum-verified: $backup_id"
  else
    echo "Encrypted backup is fresh by timestamped filename and complete file set only: $backup_id (set BACKUP_AGE_IDENTITY for checksum verification)"
  fi
else
  backup_id="${latest#manifest-}"
  backup_id="${backup_id%.json}"
  manifest="$BACKUP_DIR/$latest"
  test -z "$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "backup-failed-${backup_id}" -print -quit)"
  jq -e --arg id "$backup_id" '.backup_id == $id and (.database | type == "string") and (.gpx | type == "string") and (.database_sha256 | test("^[0-9a-f]{64}$")) and (.gpx_sha256 | test("^[0-9a-f]{64}$"))' "$manifest" >/dev/null
  db_name="$(jq -r .database "$manifest")"
  gpx_name="$(jq -r .gpx "$manifest")"
  [[ "$db_name" == "db-${backup_id}.dump" && "$gpx_name" == "gpx-${backup_id}.tar.gz" ]]
  test -s "$BACKUP_DIR/$db_name" && test -s "$BACKUP_DIR/$gpx_name"
  printf '%s  %s\n%s  %s\n' "$(jq -r .database_sha256 "$manifest")" \
    "$BACKUP_DIR/$db_name" "$(jq -r .gpx_sha256 "$manifest")" "$BACKUP_DIR/$gpx_name" |
    sha256sum -c - >/dev/null
fi

timestamp="${backup_id:0:15}"
backup_epoch="$(date -u -d "${timestamp:0:8} ${timestamp:9:2}:${timestamp:11:2}:${timestamp:13:2}" +%s)"
age="$(( $(date +%s) - backup_epoch ))"
if (( age > MAX_AGE_SECONDS || age < 0 )); then
  echo "Backup is stale or has a future timestamp: $backup_id is ${age}s old (limit ${MAX_AGE_SECONDS}s)" >&2
  exit 1
fi
echo "Backup is fresh and verified: $backup_id is ${age}s old"
