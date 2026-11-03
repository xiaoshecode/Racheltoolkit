import os
from django.core.exceptions import ImproperlyConfigured
from .settings import *

def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Missing deployment setting {name}")
    return value

SECRET_KEY = required("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5:
    raise ImproperlyConfigured("Set a unique high entropy DJANGO_SECRET_KEY")
ALLOWED_HOSTS = [required("TEACH_HOST")]
CSRF_TRUSTED_ORIGINS = [f"https://{ALLOWED_HOSTS[0]}"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"
MIDDLEWARE += ["django.middleware.clickjacking.XFrameOptionsMiddleware"]
if os.environ.get("TEACH_STORAGE_BACKEND") != "seafile":
    raise ImproperlyConfigured("Production requires private Seafile storage")
for name in ("SEAFILE_BASE_URL", "SEAFILE_LIBRARY_ID", "SEAFILE_REPO_TOKEN"):
    required(name)

