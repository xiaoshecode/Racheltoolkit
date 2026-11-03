#!/bin/sh
set -eu
umask 077
cd "$(dirname "$0")"
: "${TEACH_BACKUP_DIR:?choose an existing independent private filesystem}"
archive=$(realpath -e -- "$TEACH_BACKUP_DIR")
[ -d "$archive" ] || exit 2
stamp=$(date -u +%Y%m%dT%H%M%SZ)
docker compose --env-file .env run --rm --no-deps -v "$archive:/backups" web \
    python app/manage.py snapshot_teaching "/backups/$stamp"
