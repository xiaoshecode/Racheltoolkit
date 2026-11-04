"""Isolated CI probe of the Codespaces launcher; does not provision a Codespace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from codespaces import DATA, environment, run, PYTHON


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("This probe uses a synthetic identity on GitHub Actions only.")
    env = environment()
    opener = build_opener(NoRedirect())

    def get(path, expected):
        request = Request("http://127.0.0.1:8000" + path, headers={"Host": env["TEACH_HOST"]})
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            assert response.status == expected, (path, response.status)
            body = response.read()
            return body, response.headers

    assert json.loads(get("/health/", 200)[0]) == {"status": "ok"}
    assert b"box-sizing" in get("/static/school.css", 200)[0]
    login, headers = get("/login/", 200)
    assert b'csrfmiddlewaretoken' in login and "Secure" in headers.get("Set-Cookie", "")
    assert headers.get("X-Frame-Options") == "DENY"
    get("/me/", 302)
    for path in ("/.local/codespaces/access.txt", "/school.sqlite3", "/private/storage/",
                 "/static/%2e%2e/access.txt", "/static/%2e%2e/school.sqlite3"):
        get(path, 404)
    card = DATA / "access.txt"
    assert card.stat().st_mode & 0o777 == 0o600
    digest = hashlib.sha256(card.read_bytes()).hexdigest()
    sentinel = DATA / "probe-card.sha256"
    if args.restart:
        assert sentinel.read_text() == digest
        manifests = list((DATA / "backups").glob("*/manifest.json"))
        assert len(manifests) >= 2  # Explicit backup and setup's pre-migration backup.
        for manifest in manifests:
            database = manifest.parent / "school.sqlite3"
            with database.open("rb") as source:
                assert hashlib.file_digest(source, "sha256").hexdigest() == json.loads(manifest.read_text())["database_sha256"]
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert connection.execute("SELECT COUNT(*) FROM auth_user WHERE username = 'rachel-admin' AND is_superuser = 1").fetchone() == (1,)
            finally:
                connection.close()
    else:
        sentinel.write_text(digest)
    # The code sent to the management shell contains no credentials.
    code = "from django.contrib.auth import get_user_model; from django.conf import settings; card = (settings.RUNTIME / 'access.txt').read_text(encoding='utf-8'); password = card.split('密码：')[1].splitlines()[0]; user = get_user_model().objects.get(username='rachel-admin', is_superuser=True); assert user.check_password(password)"
    run([PYTHON, ROOT / "app/manage.py", "shell", "-c", code], env)
    print("PASS trial health, public static, secure login, private paths, administrator and restart" if args.restart
          else "PASS trial health, public static, secure login, private paths and administrator")


if __name__ == "__main__":
    main()
