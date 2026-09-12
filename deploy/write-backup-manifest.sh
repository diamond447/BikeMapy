#!/usr/bin/env bash
# Write the manifest shared by production backups and the restore drill.
set -Eeuo pipefail

: "${BACKUP_ID:?Set BACKUP_ID to the snapshot identifier}"
BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
test -d "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"

db_file="$BACKUP_DIR/db-${BACKUP_ID}.dump"
gpx_file="$BACKUP_DIR/gpx-${BACKUP_ID}.tar.gz"
manifest="$BACKUP_DIR/manifest-${BACKUP_ID}.json"
test -s "$db_file"
test -s "$gpx_file"

db_sha256="$(sha256sum "$db_file" | awk '{print $1}')"
gpx_sha256="$(sha256sum "$gpx_file" | awk '{print $1}')"
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jq -cn \
  --arg backup_id "$BACKUP_ID" \
  --arg created_at "$created_at" \
  --arg database "$(basename "$db_file")" \
  --arg database_sha256 "$db_sha256" \
  --arg gpx "$(basename "$gpx_file")" \
  --arg gpx_sha256 "$gpx_sha256" \
  '{backup_id: $backup_id, created_at: $created_at, database: $database,
    database_sha256: $database_sha256, gpx: $gpx, gpx_sha256: $gpx_sha256}' \
  > "${manifest}.part"
mv -f "${manifest}.part" "$manifest"
