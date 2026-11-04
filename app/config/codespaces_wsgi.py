"""Serve collected public assets without exposing trial uploads or SQLite."""

import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.wsgi import get_wsgi_application
from whitenoise import WhiteNoise

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.codespaces")
if os.environ["DJANGO_SETTINGS_MODULE"] != "config.codespaces":
    raise ImproperlyConfigured("The Codespaces WSGI entrypoint requires config.codespaces")


def with_public_static(application, static_root):
    return WhiteNoise(
        application,
        root=str(static_root),
        prefix="/static/",
        autorefresh=False,
        allow_all_origins=False,
    )


application = with_public_static(get_wsgi_application(), settings.STATIC_ROOT)
