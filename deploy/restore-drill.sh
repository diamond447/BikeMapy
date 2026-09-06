#!/usr/bin/env bash
# Demonstrate restoring a snapshot into disposable non-production containers.
set -Eeuo pipefail
: "${1:?Usage: restore-drill.sh BACKUP_ID}"
BACKUP_ID="$1"
BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
test -d "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
PG_IMAGE="${PG_IMAGE:-postgis/postgis:17-3.5}"
POSTGRES_USER="${POSTGRES_USER:-bikemapy}"
CONTAINER="bikemapy-restore-drill-$$"
db_file="$BACKUP_DIR/db-${BACKUP_ID}.dump"
gpx_file="$BACKUP_DIR/gpx-${BACKUP_ID}.tar.gz"
manifest="$BACKUP_DIR/manifest-${BACKUP_ID}.json"
test -s "$db_file" && test -s "$gpx_file" && test -s "$manifest"
test "$(jq -r .database_sha256 "$manifest")" = "$(sha256sum "$db_file" | awk '{print $1}')"
test "$(jq -r .gpx_sha256 "$manifest")" = "$(sha256sum "$gpx_file" | awk '{print $1}')"
cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$CONTAINER" -e POSTGRES_DB=bikemapy -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD=drill-only "$PG_IMAGE" >/dev/null
for _ in {1..60}; do
  docker exec "$CONTAINER" pg_isready -U "$POSTGRES_USER" -d bikemapy >/dev/null 2>&1 && break
  sleep 2
done
docker exec -i "$CONTAINER" pg_restore --username="$POSTGRES_USER" --clean --if-exists \
  --no-owner --dbname=bikemapy < "$db_file"
route_count="$(docker exec "$CONTAINER" psql --username="$POSTGRES_USER" --dbname=bikemapy \
  --tuples-only --no-align --command="SELECT COALESCE((SELECT COUNT(*) FROM catalogue_route), 0)")"
docker run --rm -v "$gpx_file:/backup/input.tar.gz:ro" alpine tar tzf /backup/input.tar.gz >/dev/null
evidence="$BACKUP_DIR/restore-drill-${BACKUP_ID}.json"
printf '{"backup_id":"%s","database_sha256":"%s","gpx_sha256":"%s","route_count":%s,"verified_at":"%s"}\n' \
  "$BACKUP_ID" "$(jq -r .database_sha256 "$manifest")" "$(jq -r .gpx_sha256 "$manifest")" \
  "${route_count//[[:space:]]/}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$evidence"
echo "Non-production restore drill succeeded for $BACKUP_ID (route_count=$route_count, evidence=$evidence)"
