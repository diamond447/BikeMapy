#!/usr/bin/env sh
# Run Docker Compose for the production stack from the repository root.
#
# BIKEMAPY_LEAN=true in the env file adds compose.lean.yml, so release
# commands, backup/restore scripts and cron jobs all see the same topology.
set -eu
deploy_dir="$(dirname "$0")"
env_file="${BIKEMAPY_COMPOSE_ENV_FILE:-$deploy_dir/.env.production}"
if grep -Eqx 'BIKEMAPY_LEAN=(true|1)' "$env_file" 2>/dev/null; then
  exec docker compose --env-file "$env_file" -f "$deploy_dir/compose.production.yml" \
    -f "$deploy_dir/compose.lean.yml" "$@"
fi
exec docker compose --env-file "$env_file" -f "$deploy_dir/compose.production.yml" "$@"
