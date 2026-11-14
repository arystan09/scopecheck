#!/bin/sh
# Run daily on the authorized VM; dedicated PutObject-only credentials, no key decryptor.
set -eu
umask 077
: "${BACKUP_BUCKET:?Set protected EU S3 bucket}"
: "${AGE_RECIPIENT:?Set off-host age public recipient}"
: "${AWS_DEFAULT_REGION:?Set approved EU region}"
# PGSERVICE/PGPASSFILE configure a local private connection (see operations.md).
backup_dir=$(mktemp -d)
trap 'rm -rf "$backup_dir"' EXIT HUP INT TERM
filename="$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 8).age"
uv run python scripts/backup.py --recipient "$AGE_RECIPIENT" --output "$backup_dir/$filename"
aws s3api put-object --bucket "$BACKUP_BUCKET" --key "scopecheck/$filename" \
  --body "$backup_dir/$filename" --server-side-encryption AES256 \
  --checksum-algorithm SHA256 >/dev/null
printf 'Encrypted backup uploaded: %s\n' "$filename"
