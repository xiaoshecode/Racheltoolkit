#!/bin/sh
# Called by the systemd unit only AFTER it acquired the shared deployment lock.
set -eu
cd "$(dirname "$0")/../.."
if [ -e .local/deploy-incomplete ]; then
    printf '%s\n' 'Maintenance blocked: deployment is incomplete; finish or repair the deployment first.' >&2
    exit 75
fi
case "${1:-}" in
    previews) exec .venv/bin/python app/manage.py render_previews --limit 20 ;;
    backup) exec /bin/sh deploy/backup.sh ;;
    *) printf '%s\n' 'Expected maintenance task: previews or backup' >&2; exit 2 ;;
esac
