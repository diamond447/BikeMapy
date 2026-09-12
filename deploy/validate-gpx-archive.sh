#!/usr/bin/env bash
# Verify that a GPX archive is readable and rooted at the production volume.
set -Eeuo pipefail

: "${1:?Usage: validate-gpx-archive.sh ARCHIVE}"
archive="$1"
test -s "$archive"
python3 "$(dirname "$0")/../scripts/validate_gpx_archive.py" "$archive"
