#!/bin/sh
# Keep Nginx access and diagnostic output machine-readable. Access logs stay
# on stdout; this adapter converts Nginx's otherwise plain stderr lines.
set -eu

error_pipe="/tmp/nginx-error.$$"
cleanup() {
  rm -f "$error_pipe"
}
trap cleanup EXIT INT TERM
mkfifo "$error_pipe"
awk '
  function escape(value) {
    # Nginx native diagnostics may contain client addresses, request queries,
    # bearer/token values, and upstream URLs. Keep only the path and redact
    # those fields before serializing the diagnostic line.
    gsub(/\?[^ ]*/, "", value)
    gsub(/[Tt][Oo][Kk][Ee][Nn][=:][^ ,]*/, "token=[REDACTED]", value)
    gsub(/[Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]:[^ ,]*/, "authorization:[REDACTED]", value)
    gsub(/upstream: [^,]*/, "upstream: [REDACTED]", value)
    gsub(/[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*/, "[REDACTED_IP]", value)
    gsub(/[0-9A-Fa-f][0-9A-Fa-f:]*:[0-9A-Fa-f:]+/, "[REDACTED_IP]", value)
    gsub(/\\/, "\\\\", value)
    gsub(/"/, "\\\"", value)
    gsub(/[[:cntrl:]]/, " ", value)
    return value
  }
  { printf "{\"level\":\"error\",\"logger\":\"nginx\",\"message\":\"%s\"}\n", escape($0); fflush() }
' < "$error_pipe" &
logger_pid=$!
nginx -g 'daemon off;' 2>"$error_pipe" &
nginx_pid=$!
forward_signal() {
  kill -TERM "$nginx_pid" 2>/dev/null || true
}
trap 'forward_signal; cleanup' INT TERM
set +e
wait "$nginx_pid"
status=$?
set -e
wait "$logger_pid" || true
exit "$status"
