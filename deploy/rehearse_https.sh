#!/usr/bin/env bash
# Ephemeral CI only. Reserved .test DNS and dummy secrets scope this run to the runner.
set -euo pipefail
cd "$(dirname "$0")"
test "${GITHUB_ACTIONS:-}" = true
trap 'docker compose --env-file .env down --volumes || true' EXIT
umask 077
mkdir -p .local
sudo install -d -o 10001 -g 10001 -m 0700 "$PWD/.local/host-data" "$PWD/.local/independent-backup"
printf 'TEACH_RELEASE=rehearsal\nTEACH_HOST=teaching.test\nTEACH_STORAGE_BACKEND=seafile\nSEAFILE_BASE_URL=https://seafile.test\nSEAFILE_LIBRARY_ID=fixture\nSEAFILE_REPO_TOKEN=unused-synthetic-value\nDJANGO_SECRET_KEY=%s\n' "$(openssl rand -hex 48)" > .env
printf 'COMPOSE_FILE=compose.yml:compose.host-data.yml:.local/portal.yml\nTEACH_PORTAL_ORIGIN=https://portal.test\nTEACH_PERSIST_DIR=%s\n' "$PWD/.local/host-data" >> .env
# Use Caddy's private CA in CI only. Production obtains its certificate through ACME.
sed -i '1a\    tls internal' Caddyfile
cat >> Caddyfile <<'EOF'
portal.test {
    tls internal
    root * /srv/portal
    handle_path /Racheltoolkit/* {
        file_server
    }
}
EOF
cat > .local/portal.yml <<'EOF'
services:
  https:
    volumes:
      - ../portal:/srv/portal:ro
EOF
printf '{"origin":"https://teaching.test"}\n' > ../portal/config.json
printf '\n127.0.0.1 teaching.test portal.test\n' | sudo tee -a /etc/hosts >/dev/null
docker compose --env-file .env up -d --build
for attempt in $(seq 1 45); do
    if docker compose exec -T https cat /data/caddy/pki/authorities/local/root.crt > .local/fixture-ca.pem; then break; fi
    sleep 2
done
curl_ci() { curl --cacert .local/fixture-ca.pem -sSf --noproxy '*' "$@"; }
for attempt in $(seq 1 45); do
    if curl_ci https://teaching.test/health/ | grep -q '"ok"'; then break; fi
    sleep 2
done
curl_ci https://teaching.test/health/ | grep -q '"ok"'
curl_ci https://teaching.test/static/school.css | grep -q 'box-sizing'
docker compose exec -T web python app/manage.py shell -c 'from school.models import Term; Term.objects.create(code="restart-sentinel")'
docker compose restart web
for attempt in $(seq 1 25); do
    if curl_ci https://teaching.test/api/public/courses/ | grep -q courses; then break; fi
    sleep 2
done
curl_ci https://teaching.test/api/public/courses/ | grep -q courses
docker compose exec -T web python app/manage.py shell -c 'from school.models import Term; assert Term.objects.filter(code="restart-sentinel").exists()'
docker compose exec -T web python app/manage.py shell -c '
from django.utils import timezone
from school.models import Course
from catalog.models import CourseRevision, CoursePublication, Section
c = Course.objects.create(code="FIELD", title="Synthetic field exercise")
r = CourseRevision.objects.create(course=c, seq=1, published_at=timezone.now(), title=c.title)
Section.objects.create(revision=r, position=1, title="First chapter")
CoursePublication.objects.create(course=c, revision=r)'
SSL_CERT_FILE="$PWD/.local/fixture-ca.pem" NO_PROXY="portal.test,teaching.test" \
    python3 verify_entrypoints.py --portal https://portal.test/Racheltoolkit/ --teaching-origin https://teaching.test
TEACH_BACKUP_DIR="$PWD/.local/independent-backup" sh backup.sh
sudo find "$PWD/.local/independent-backup" -name manifest.json | grep -q manifest.json
printf 'PASS isolated HTTPS, production settings, static CSS, restart and persisted SQLite record\n'

