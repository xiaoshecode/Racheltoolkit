"""HTTPS deployment on a persistent Linux host with private local object storage.

Provision TEACH_DATA_DIR before startup and mount it into every web/worker
process. Only the staticfiles child may be exposed by the reverse proxy.
"""

import ipaddress
import os
from pathlib import Path
import re
import stat

from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Missing production-local setting {name}")
    return value


def validated_host(value):
    """Accept one HTTPS authority, without wildcards, URL parts or a port."""
    if value.startswith("[") and value.endswith("]") and "%" not in value:
        try:
            return f"[{ipaddress.IPv6Address(value[1:-1]).compressed}]"
        except ValueError:
            pass
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        pass
    host = value.lower()
    labels = host.split(".")
    if (
        len(host) <= 253
        and len(labels) >= 2
        and not labels[-1].isdigit()
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        return host
    raise ImproperlyConfigured("TEACH_HOST must be one DNS hostname or IP address, without a port")


_host = validated_host(required("TEACH_HOST"))
_secret = required("DJANGO_SECRET_KEY")
if len(_secret) < 50 or len(set(_secret)) < 5 or _secret.startswith("django-insecure-"):
    raise ImproperlyConfigured("Set a unique high entropy DJANGO_SECRET_KEY")
if required("TEACH_STORAGE_BACKEND") != "local":
    raise ImproperlyConfigured("Production-local requires explicit TEACH_STORAGE_BACKEND=local")

# Validate before importing the shared settings, which otherwise creates a
# default runtime directory. An absent volume must not silently become an empty
# installation on the container's ephemeral filesystem.
_configured_runtime = Path(required("TEACH_DATA_DIR"))
if not _configured_runtime.is_absolute() or not _configured_runtime.is_dir():
    raise ImproperlyConfigured("TEACH_DATA_DIR must be an existing absolute private data directory")
_runtime = _configured_runtime.resolve()
if _runtime == Path(_runtime.anchor):
    raise ImproperlyConfigured("TEACH_DATA_DIR cannot be a filesystem root")
if os.name == "posix":
    _runtime_stat = _runtime.stat()
    if _runtime_stat.st_uid != os.geteuid() or stat.S_IMODE(_runtime_stat.st_mode) & 0o077:
        raise ImproperlyConfigured("TEACH_DATA_DIR must be owned by the service user with mode 0700")
if not os.access(_runtime, os.R_OK | os.W_OK | os.X_OK):
    raise ImproperlyConfigured("The service user must be able to read and write TEACH_DATA_DIR")

_database = _runtime / "school.sqlite3"
if os.environ.get("TEACH_DB"):
    _configured_database = Path(os.environ["TEACH_DB"])
    if not _configured_database.is_absolute() or _configured_database.resolve() != _database:
        raise ImproperlyConfigured("TEACH_DB must use TEACH_DATA_DIR/school.sqlite3")
for _child in ("school.sqlite3", "private", "private/storage", "staticfiles"):
    if (_runtime / _child).is_symlink():
        raise ImproperlyConfigured("Production data and static paths cannot be symbolic links")

from .settings import *  # noqa: E402,F403


SECRET_KEY = _secret
DEBUG = False
DEPLOYED = True
CODESPACES_TRIAL = False
ALLOWED_HOSTS = [_host]
CSRF_TRUSTED_ORIGINS = [f"https://{_host}"]

# The web listener must stay on the private Compose network. Caddy replaces the
# forwarded scheme; Waitress must accept only x-forwarded-proto from that proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = False
USE_X_FORWARDED_PORT = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 86400
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
# Apply HSTS only to this teaching host. Other domains below it are not managed
# by this installation, and deployment does not enroll the hostname in preload.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]
X_FRAME_OPTIONS = "DENY"
MIDDLEWARE = [*MIDDLEWARE, "django.middleware.clickjacking.XFrameOptionsMiddleware"]

RUNTIME = _runtime
DATABASES = {
    **DATABASES,
    "default": {
        **DATABASES["default"],
        "NAME": _database,
        "TEST": {"NAME": RUNTIME / f"test-{os.getpid()}.sqlite3"},
    },
}
TEACH_STORAGE_BACKEND = "local"
LOCAL_STORAGE_ROOT = RUNTIME / "private" / "storage"
STATIC_ROOT = RUNTIME / "staticfiles"
FILE_UPLOAD_TEMP_DIR = str(RUNTIME)
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
# collectstatic otherwise inherits the private upload modes above. Its output
# lives on the separate public-static volume and must be readable by Caddy,
# which deliberately has no permission to bypass filesystem ownership checks.
STORAGES = {
    **global_settings.STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        "OPTIONS": {
            "file_permissions_mode": 0o644,
            "directory_permissions_mode": 0o755,
        },
    },
}
