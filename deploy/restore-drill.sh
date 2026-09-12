#!/usr/bin/env bash
# Demonstrate restoring a snapshot into disposable non-production containers.
set -Eeuo pipefail
: "${1:?Usage: restore-drill.sh BACKUP_ID}"
BACKUP_ID="$1"
BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backup}"
test -d "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
PG_IMAGE="${PG_IMAGE:-postgis/postgis:17-3.5}"
APP_IMAGE="${APP_IMAGE:-bikemapy-restore-drill-backend}"
POSTGRES_USER="${POSTGRES_USER:-bikemapy}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-drill-only}"
CONTAINER="bikemapy-restore-drill-db-$$"
APP_CONTAINER="bikemapy-restore-drill-app-$$"
NETWORK="bikemapy-restore-drill-network-$$"
GPX_VOLUME="bikemapy-restore-drill-gpx-$$"
db_file="$BACKUP_DIR/db-${BACKUP_ID}.dump"
gpx_file="$BACKUP_DIR/gpx-${BACKUP_ID}.tar.gz"
manifest="$BACKUP_DIR/manifest-${BACKUP_ID}.json"
test -s "$db_file" && test -s "$gpx_file" && test -s "$manifest"
database_sha256="$(jq -er .database_sha256 "$manifest")"
gpx_sha256="$(jq -er .gpx_sha256 "$manifest")"
test "$database_sha256" = "$(sha256sum "$db_file" | awk '{print $1}')"
test "$gpx_sha256" = "$(sha256sum "$gpx_file" | awk '{print $1}')"
bash "$(dirname "$0")/validate-gpx-archive.sh" "$gpx_file"

cleanup() {
  docker rm -f "$APP_CONTAINER" "$CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  docker volume rm "$GPX_VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$NETWORK" >/dev/null
docker volume create "$GPX_VOLUME" >/dev/null
docker run -d --name "$CONTAINER" --network "$NETWORK" --network-alias db \
  -e POSTGRES_DB=bikemapy -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" "$PG_IMAGE" >/dev/null

database_ready=0
for _ in {1..60}; do
  if docker exec "$CONTAINER" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d bikemapy \
    -c "SELECT 1" >/dev/null 2>&1; then
    database_ready=1
    break
  fi
  sleep 2
done
if (( ! database_ready )); then
  echo "Disposable PostGIS database did not become queryable over TCP" >&2
  docker logs "$CONTAINER" >&2 || true
  exit 1
fi

docker exec -i "$CONTAINER" pg_restore --username="$POSTGRES_USER" --clean --if-exists \
  --no-owner --exit-on-error --dbname=bikemapy < "$db_file"
route_count="$(docker exec "$CONTAINER" psql --username="$POSTGRES_USER" --dbname=bikemapy \
  --tuples-only --no-align --command="SELECT COUNT(*) FROM catalogue_route")"
route_count="${route_count//[[:space:]]/}"
test "$route_count" -gt 0
migration_count="$(docker exec "$CONTAINER" psql --username="$POSTGRES_USER" --dbname=bikemapy \
  --tuples-only --no-align --command="SELECT COUNT(*) FROM django_migrations")"
migration_count="${migration_count//[[:space:]]/}"
test "$migration_count" -gt 0

# Restore the archive into a new volume, matching the production
# /app/storage/media root. The archive validator has already rejected paths
# outside media/ and traversal entries.
archive_name="$(basename "$gpx_file")"
docker run --rm -v "$GPX_VOLUME:/data" -v "$BACKUP_DIR:/backup:ro" alpine \
  sh -c 'set -eu; find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} +; tar xzf "/backup/$1" -C /data' \
  sh "$archive_name"

# Read every live storage reference and validate it against the restored
# volume. A missing, corrupt, checksum-mismatched, or semantically invalid
# payload must fail the drill rather than produce partial evidence.
reference_rows="$(docker run --rm --network "$NETWORK" \
  -e POSTGRES_DB=bikemapy -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_HOST=db \
  "$APP_IMAGE" python scripts/query_restore_gpx_references.py)"
test "$(jq -er 'length' <<< "$reference_rows")" -gt 0
printf '%s' "$reference_rows" | docker run --rm -i -v "$GPX_VOLUME:/app/storage:ro" \
  "$APP_IMAGE" python scripts/validate_restore_gpx_references.py --storage-root /app/storage/media

# Endpoint checks must exercise a currently approved version. Historical
# versions remain part of the exhaustive validation above, but are not the
# payload selected by the public route endpoint.
approved_row="$(docker run --rm --network "$NETWORK" \
  -e POSTGRES_DB=bikemapy -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_HOST=db \
  "$APP_IMAGE" python scripts/query_restore_gpx_references.py --approved)"
test "$(jq -er 'length' <<< "$approved_row")" -eq 1
route_id="$(jq -er '.[0][0]' <<< "$approved_row")"
gpx_key="$(jq -er '.[0][1]' <<< "$approved_row")"
expected_gpx_sha="$(jq -er '.[0][2]' <<< "$approved_row")"
restored_gpx_sha="$expected_gpx_sha"

# Start the real application image against the restored targets. These checks
# cover startup, database/cache readiness, representative catalogue reads,
# and the GPX endpoint serving the exact restored bytes.
docker run -d --name "$APP_CONTAINER" --network "$NETWORK" \
  -e POSTGRES_DB=bikemapy -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_HOST=db \
  -e DJANGO_DEBUG=true -e GPX_REDISTRIBUTION_APPROVED=true \
  -e DJANGO_MEDIA_ROOT=/app/storage/media -v "$GPX_VOLUME:/app/storage" "$APP_IMAGE" \
  uv run --locked --no-dev python backend/manage.py runserver 0.0.0.0:8000 --noreload >/dev/null
application_ready=0
for _ in {1..30}; do
  if docker exec "$APP_CONTAINER" python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health/live/", timeout=2)' \
    >/dev/null 2>&1; then
    application_ready=1
    break
  fi
  sleep 2
done
if (( ! application_ready )); then
  echo "Restored application did not start or become live" >&2
  docker logs "$APP_CONTAINER" >&2 || true
  exit 1
fi
docker exec -e "DRILL_ROUTE_ID=$route_id" -e "DRILL_GPX_SHA=$expected_gpx_sha" \
  "$APP_CONTAINER" python -c '
import hashlib
import json
import os
import urllib.request

base = "http://127.0.0.1:8000"
paths = ["/health/live/", "/health/ready/", "/health/crawler/", "/api/v1/", "/api/v1/routes/"]
route_id = os.environ["DRILL_ROUTE_ID"]
for path in paths + [f"/api/v1/routes/{route_id}/"]:
    with urllib.request.urlopen(base + path, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        body = response.read()
        if path == "/api/v1/routes/" and not json.loads(body)["results"]:
            raise RuntimeError("restored route catalogue is empty")
with urllib.request.urlopen(base + f"/api/v1/routes/{route_id}/gpx/", timeout=5) as response:
    payload = response.read()
    if response.status != 200 or hashlib.sha256(payload).hexdigest() != os.environ["DRILL_GPX_SHA"]:
        raise RuntimeError("GPX endpoint did not return the restored bytes")
'

evidence="$BACKUP_DIR/restore-drill-${BACKUP_ID}.json"
jq -cn \
  --arg backup_id "$BACKUP_ID" \
  --arg database_sha256 "$database_sha256" \
  --arg gpx_sha256 "$gpx_sha256" \
  --arg gpx_storage_key "$gpx_key" \
  --arg restored_gpx_sha256 "$restored_gpx_sha" \
  --arg route_id "$route_id" \
  --argjson route_count "$route_count" \
  --argjson migration_count "$migration_count" \
  --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{backup_id: $backup_id, database_sha256: $database_sha256, gpx_sha256: $gpx_sha256,
    gpx_storage_key: $gpx_storage_key, restored_gpx_sha256: $restored_gpx_sha256,
    route_id: $route_id, route_count: $route_count, migration_count: $migration_count,
    application_endpoints: ["/health/live/", "/health/ready/", "/health/crawler/", "/api/v1/",
      "/api/v1/routes/", "/api/v1/routes/<route-id>/", "/api/v1/routes/<route-id>/gpx/"],
    verified_at: $verified_at}' > "$evidence"
echo "Non-production restore drill succeeded for $BACKUP_ID (route_count=$route_count, evidence=$evidence)"
