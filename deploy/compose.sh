#!/usr/bin/env sh
# Docker Compose for the production stack; BIKEMAPY_LEAN=true adds compose.lean.yml.
set -eu
deploy_dir="$(dirname "$0")"
env_file="${BIKEMAPY_COMPOSE_ENV_FILE:-$deploy_dir/.env.production}"
if grep -Eqx 'BIKEMAPY_LEAN=(true|1)' "$env_file" 2>/dev/null; then
  exec docker compose --env-file "$env_file" -f "$deploy_dir/compose.production.yml" \
    -f "$deploy_dir/compose.lean.yml" "$@"
fi
exec docker compose --env-file "$env_file" -f "$deploy_dir/compose.production.yml" "$@"
