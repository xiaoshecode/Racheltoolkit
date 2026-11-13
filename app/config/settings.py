import os
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("TEACH_DATA_DIR", BASE_DIR / ".local"))
RUNTIME.mkdir(parents=True, exist_ok=True)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "development-only-key")
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
PUBLIC_PORTAL_ORIGIN = os.environ.get("TEACH_PORTAL_ORIGIN", "https://xiaoshecode.github.io")
portal = urlsplit(PUBLIC_PORTAL_ORIGIN)
if (
    portal.scheme != "https"
    or not portal.hostname
    or portal.username is not None
    or portal.password is not None
    or PUBLIC_PORTAL_ORIGIN != f"https://{portal.netloc}"
):
    raise ImproperlyConfigured(
        "TEACH_PORTAL_ORIGIN must be the HTTPS origin of the public Pages site"
    )
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
    "school",
    "storage",
    "operations",
    "submissions",
    "catalog",
    "grading",
    "dashboard",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "config.middleware.PrivateResponses",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "app/templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "school.context_processors.workspace",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("TEACH_DB", RUNTIME / "school.sqlite3"),
        "OPTIONS": {"timeout": 10, "transaction_mode": "IMMEDIATE"},
        "TEST": {"NAME": RUNTIME / f"test-{os.getpid()}.sqlite3"},
    }
}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{validator}"}
    for validator in (
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    )
]
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = RUNTIME / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "app/static"] if (BASE_DIR / "app/static").is_dir() else []
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/me/"
DATA_UPLOAD_MAX_MEMORY_SIZE = 22 * 1024 * 1024
FILE_UPLOAD_TEMP_DIR = str(RUNTIME)
TEST_RUNNER = "config.testing.ProjectRunner"

# Optional API credentials stay on the teaching server.
from .assistant_credentials import local_key

try:
    DEEPSEEK_API_KEY = local_key(RUNTIME)
except ValueError:
    raise ImproperlyConfigured("Cannot read the private local DeepSeek key; check its format and permissions.") from None
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "").strip() or "auto"


def assistant_integer(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ImproperlyConfigured(f"{name} must be between {minimum} and {maximum}")
    return value


DEEPSEEK_TIMEOUT_SECONDS = assistant_integer("DEEPSEEK_TIMEOUT_SECONDS", 60, 5, 90)
DEEPSEEK_MAX_TOKENS = assistant_integer("DEEPSEEK_MAX_TOKENS", 4096, 256, 16384)
