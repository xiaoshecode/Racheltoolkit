"""HTTPS trial deployment in one GitHub Codespace, separate from production."""

import re

from .settings import *


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Missing Codespaces setting {name}")
    return value


if os.environ.get("CODESPACES") != "true":
    raise ImproperlyConfigured("Codespaces settings require CODESPACES=true")

SECRET_KEY = required("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("Set a unique high entropy DJANGO_SECRET_KEY")
DEBUG = False
DEPLOYED = True
CODESPACES_TRIAL = True

codespace_name = required("CODESPACE_NAME")
forwarding_domain = required("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
if (
    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?", codespace_name)
    or forwarding_domain != "app.github.dev"
):
    raise ImproperlyConfigured("Use the actual GitHub Codespaces forwarding hostname")
expected_host = f"{codespace_name}-8000.{forwarding_domain}"
if required("TEACH_HOST") != expected_host:
    raise ImproperlyConfigured("TEACH_HOST must match this Codespace's forwarded port 8000")
ALLOWED_HOSTS = [expected_host]
CSRF_TRUSTED_ORIGINS = [f"https://{expected_host}"]

# Waitress sets wsgi.url_scheme=https for GitHub's HTTPS forwarding. Do not
# accept client-supplied proxy headers as proof that an arbitrary URL is secure.
SECURE_PROXY_SSL_HEADER = None
USE_X_FORWARDED_HOST = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 300
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
# GitHub owns this temporary forwarding domain. Apply HSTS to this host only;
# do not opt its subdomains into HSTS or advertise a browser preload request.
# All other deployment checks remain enabled, including HTTPS and cookie checks.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]
X_FRAME_OPTIONS = "DENY"
MIDDLEWARE = [*MIDDLEWARE, "django.middleware.clickjacking.XFrameOptionsMiddleware"]
WSGI_APPLICATION = "config.codespaces_wsgi.application"

# /workspaces survives a Codespace rebuild. Keep the database, object files,
# generated secret and static output together in the gitignored trial directory.
RUNTIME = (BASE_DIR / ".local" / "codespaces").resolve()
for name, expected in (
    ("TEACH_DATA_DIR", RUNTIME),
    ("TEACH_DB", RUNTIME / "school.sqlite3"),
):
    value = os.environ.get(name)
    if value and Path(value).resolve() != expected:
        raise ImproperlyConfigured(f"{name} must use the persistent Codespaces trial directory")
RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
DATABASES = {
    **DATABASES,
    "default": {
        **DATABASES["default"],
        "NAME": RUNTIME / "school.sqlite3",
        "TEST": {"NAME": RUNTIME / f"test-{os.getpid()}.sqlite3"},
    },
}
LOCAL_STORAGE_ROOT = RUNTIME / "private" / "storage"
STATIC_ROOT = RUNTIME / "staticfiles"
FILE_UPLOAD_TEMP_DIR = str(RUNTIME)

TEACH_STORAGE_BACKEND = os.environ.get("TEACH_STORAGE_BACKEND", "local").strip()
if TEACH_STORAGE_BACKEND not in ("local", "seafile"):
    raise ImproperlyConfigured("Codespaces storage must be local or explicitly configured Seafile")
if TEACH_STORAGE_BACKEND == "seafile":
    for name in ("SEAFILE_BASE_URL", "SEAFILE_LIBRARY_ID", "SEAFILE_REPO_TOKEN"):
        required(name)
    seafile_url = urlsplit(os.environ["SEAFILE_BASE_URL"])
    if (
        seafile_url.scheme != "https"
        or not seafile_url.hostname
        or seafile_url.username is not None
        or seafile_url.password is not None
        or seafile_url.query
        or seafile_url.fragment
        or "\\" in os.environ["SEAFILE_BASE_URL"]
    ):
        raise ImproperlyConfigured("Seafile requires a private HTTPS service URL")
