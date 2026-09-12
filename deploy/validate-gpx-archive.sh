#!/usr/bin/env bash
# Verify that a GPX archive is readable and rooted at the production volume.
set -Eeuo pipefail

: "${1:?Usage: validate-gpx-archive.sh ARCHIVE}"
archive="$1"
test -s "$archive"

docker run --rm -v "$archive:/backup/input.tar.gz:ro" alpine sh -c \
  'set -eu
   listing=/tmp/gpx-archive.list
   # Keep tar separate from the path loop: a failed listing must stop restore.
   tar tzf /backup/input.tar.gz > "$listing"
   while IFS= read -r entry; do
     case "$entry" in
       .|./|media|media/*|./media|./media/*)
         case "$entry" in
           *"/../"*|*"/.."|../*|..) echo "GPX archive contains traversal: $entry" >&2; exit 1 ;;
         esac ;;
       *) echo "GPX archive contains a path outside media/: $entry" >&2; exit 1 ;;
     esac
   done < "$listing"'
