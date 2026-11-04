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


def with_codespaces_host(application, canonical_host):
    def forwarded_application(environ, start_response):
        # The Codespaces tunnel sends this exact internal Host. Use only the
        # configured external identity, never a client-supplied forwarded host.
        # Other hosts still reach Django's normal ALLOWED_HOSTS validation.
        if environ.get("HTTP_HOST") == "localhost:8000":
            environ["HTTP_HOST"] = canonical_host
            # The tunnel also rewrites a same-origin browser POST to this
            # exact local origin. Preserve all other origins for CSRF checks.
            if environ.get("HTTP_ORIGIN") == "http://localhost:8000":
                environ["HTTP_ORIGIN"] = f"https://{canonical_host}"
        return application(environ, start_response)

    return forwarded_application


application = with_codespaces_host(
    with_public_static(get_wsgi_application(), settings.STATIC_ROOT),
    settings.ALLOWED_HOSTS[0],
)
