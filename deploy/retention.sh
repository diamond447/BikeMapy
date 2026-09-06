#!/usr/bin/env bash
# Retain a bounded number of complete recovery points without removing newest.
set -Eeuo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
KEEP_SNAPSHOTS="${KEEP_SNAPSHOTS:-30}"
test -d "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
mapfile -t manifests < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'manifest-*.json' -o -name 'manifest-*.json.age' \) -printf '%f\n' | sort -r)
(( ${#manifests[@]} > KEEP_SNAPSHOTS )) || exit 0
# Refuse cleanup if the newest recovery point is incomplete or unverifiable.
BACKUP_DIR="$BACKUP_DIR" "$(dirname "$0")/check-backup-freshness.sh"
for manifest in "${manifests[@]:KEEP_SNAPSHOTS}"; do
  id="${manifest#manifest-}"
  id="${id%.json.age}"
  id="${id%.json}"
  rm -f -- "$BACKUP_DIR/manifest-${id}.json" "$BACKUP_DIR/manifest-${id}.json.age" \
    "$BACKUP_DIR/db-${id}.dump" "$BACKUP_DIR/db-${id}.dump.age" \
    "$BACKUP_DIR/gpx-${id}.tar.gz" "$BACKUP_DIR/gpx-${id}.tar.gz.age" \
    "$BACKUP_DIR/restore-drill-${id}.json" "$BACKUP_DIR/backup-failed-${id}"
done
echo "Retained newest $KEEP_SNAPSHOTS recovery points in $BACKUP_DIR"
