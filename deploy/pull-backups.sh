#!/usr/bin/env bash
# Pull snapshots over SSH and encrypt them at rest on the laptop.
set -Eeuo pipefail
: "${BACKUP_SSH_TARGET:?Set BACKUP_SSH_TARGET to the restricted backup account and host}"
: "${BACKUP_REMOTE_DIR:?Set BACKUP_REMOTE_DIR to the host backup directory}"
: "${LAPTOP_BACKUP_DIR:?Set LAPTOP_BACKUP_DIR to an encrypted-disk directory}"
: "${BACKUP_AGE_RECIPIENT:?Set BACKUP_AGE_RECIPIENT to the laptop age public key}"
[[ "$BACKUP_REMOTE_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || {
  echo "BACKUP_REMOTE_DIR must be an absolute path without shell metacharacters" >&2
  exit 2
}
SSH_KEY_ARGS=()
if [[ -n "${BACKUP_SSH_KEY:-}" ]]; then SSH_KEY_ARGS=(-i "$BACKUP_SSH_KEY" -o IdentitiesOnly=yes); fi
mkdir -p "$LAPTOP_BACKUP_DIR"
rm -f "$LAPTOP_BACKUP_DIR"/.db-*.part "$LAPTOP_BACKUP_DIR"/.gpx-*.part \
  "$LAPTOP_BACKUP_DIR"/.manifest-*.part
ssh "${SSH_KEY_ARGS[@]}" "$BACKUP_SSH_TARGET" \
  "find $BACKUP_REMOTE_DIR -maxdepth 1 -type f \( -name 'db-*.dump' -o -name 'gpx-*.tar.gz' -o -name 'manifest-*.json' \) -printf '%f\\n'" |
while IFS= read -r name; do
  [[ -n "$name" ]] || continue
  [[ "$name" != */* && "$name" != ..* ]] || exit 2
  temporary="$LAPTOP_BACKUP_DIR/.${name}.part"
  rm -f "$temporary"
  cleanup_temporary() { rm -f "$temporary"; }
  trap cleanup_temporary EXIT INT TERM
  ssh "${SSH_KEY_ARGS[@]}" "$BACKUP_SSH_TARGET" "cat $BACKUP_REMOTE_DIR/$name" |
    age -r "$BACKUP_AGE_RECIPIENT" -o "$temporary"
  mv -f "$temporary" "$LAPTOP_BACKUP_DIR/${name}.age"
  trap - EXIT INT TERM
done
echo "Encrypted backup pull completed in $LAPTOP_BACKUP_DIR"
