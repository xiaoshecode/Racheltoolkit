import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import unquote
from wsgiref.util import setup_testing_defaults

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.wsgi import get_wsgi_application
from django.test import SimpleTestCase, override_settings


def load_config(filename, environment):
    path = Path(__file__).parent / filename
    spec = importlib.util.spec_from_file_location(f"config._test_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, environment, clear=True):
        spec.loader.exec_module(module)
    return module


class CodespacesSettingsTests(SimpleTestCase):
    def setUp(self):
        self.environment = {
            "CODESPACES": "true",
            "CODESPACE_NAME": "test-classroom-123456",
            "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev",
            "TEACH_HOST": "test-classroom-123456-8000.app.github.dev",
            "DJANGO_SECRET_KEY": "synthetic-only-test-key-" + "0123456789abcdef" * 4,
        }

    def load(self, **changes):
        return load_config("codespaces.py", self.environment | changes)

    def test_https_and_storage_are_confined_to_the_trial(self):
        configured = self.load()
        runtime = (settings.BASE_DIR / ".local" / "codespaces").resolve()
        self.assertFalse(configured.DEBUG)
        self.assertEqual(configured.ALLOWED_HOSTS, [self.environment["TEACH_HOST"]])
        self.assertEqual(configured.CSRF_TRUSTED_ORIGINS, ["https://" + self.environment["TEACH_HOST"]])
        self.assertTrue(configured.SECURE_SSL_REDIRECT)
        self.assertTrue(configured.SESSION_COOKIE_SECURE)
        self.assertTrue(configured.CSRF_COOKIE_SECURE)
        self.assertIsNone(configured.SECURE_PROXY_SSL_HEADER)
        self.assertFalse(configured.USE_X_FORWARDED_HOST)
        self.assertEqual(configured.TEACH_STORAGE_BACKEND, "local")
        self.assertEqual(configured.RUNTIME, runtime)
        self.assertEqual(configured.DATABASES["default"]["NAME"], runtime / "school.sqlite3")
        self.assertEqual(configured.LOCAL_STORAGE_ROOT, runtime / "private" / "storage")
        self.assertEqual(configured.STATIC_ROOT, runtime / "staticfiles")

    def test_requires_actual_codespace_and_exact_forwarded_host(self):
        for changes in (
            {"CODESPACES": "false"},
            {"CODESPACE_NAME": ""},
            {"CODESPACE_NAME": "../test"},
            {"GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "example.com"},
            {"TEACH_HOST": "other-classroom-8000.app.github.dev"},
            {"TEACH_HOST": ".app.github.dev"},
            {"TEACH_HOST": self.environment["TEACH_HOST"] + ":8000"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ImproperlyConfigured):
                self.load(**changes)

    def test_requests_require_exact_host_and_do_not_trust_forwarded_scheme(self):
        configured = self.load()
        with override_settings(
            DEBUG=False,
            ALLOWED_HOSTS=configured.ALLOWED_HOSTS,
            MIDDLEWARE=configured.MIDDLEWARE,
            SECURE_SSL_REDIRECT=True,
            SECURE_PROXY_SSL_HEADER=None,
        ):
            host = self.environment["TEACH_HOST"]
            response = self.client.get("/health/", secure=True, HTTP_HOST=host)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(self.client.get("/health/", secure=True, HTTP_HOST="other.app.github.dev").status_code, 400)
            response = self.client.get("/health/", HTTP_HOST=host, HTTP_X_FORWARDED_PROTO="https")
            self.assertEqual(response.status_code, 301)
            self.assertEqual(response.headers["Location"], f"https://{host}/health/")

    def test_missing_or_weak_secret_is_rejected(self):
        for value in ("", "development-only-key", "a" * 64, "django-insecure-" + "0123456789abcdef" * 4):
            with self.subTest(length=len(value)), self.assertRaises(ImproperlyConfigured):
                self.load(DJANGO_SECRET_KEY=value)

    def test_rejects_database_or_files_outside_persistent_trial_directory(self):
        for key in ("TEACH_DATA_DIR", "TEACH_DB"):
            with self.subTest(key=key), self.assertRaises(ImproperlyConfigured):
                self.load(**{key: str(settings.BASE_DIR / ".local" / "elsewhere")})
        runtime = settings.BASE_DIR / ".local" / "codespaces"
        self.load(TEACH_DATA_DIR=str(runtime), TEACH_DB=str(runtime / "school.sqlite3"))

    def test_seafile_requires_explicit_selection_and_complete_https_configuration(self):
        cloud = {
            "TEACH_STORAGE_BACKEND": "seafile",
            "SEAFILE_BASE_URL": "https://files.example.test",
            "SEAFILE_LIBRARY_ID": "synthetic-library",
            "SEAFILE_REPO_TOKEN": "synthetic-repository-token",
        }
        self.assertEqual(self.load(**cloud).TEACH_STORAGE_BACKEND, "seafile")
        self.assertEqual(self.load(**(cloud | {"TEACH_STORAGE_BACKEND": "local"})).TEACH_STORAGE_BACKEND, "local")
        for name in ("SEAFILE_BASE_URL", "SEAFILE_LIBRARY_ID", "SEAFILE_REPO_TOKEN"):
            with self.subTest(missing=name), self.assertRaises(ImproperlyConfigured):
                self.load(**(cloud | {name: ""}))
        for url in ("http://files.example.test", "https://user:password@files.example.test", "https://files.example.test?token=x"):
            with self.subTest(url=url), self.assertRaises(ImproperlyConfigured):
                self.load(**(cloud | {"SEAFILE_BASE_URL": url}))
        with self.assertRaises(ImproperlyConfigured):
            self.load(TEACH_STORAGE_BACKEND="unknown")


class CodespacesStaticTests(SimpleTestCase):
    def test_wsgi_exposes_only_collected_static_files(self):
        def fallback(environ, start_response):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]

        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            public = runtime / "staticfiles"
            public.mkdir()
            (public / "site.css").write_text("body { color: navy; }", encoding="utf-8")
            (runtime / "school.sqlite3").write_bytes(b"private database sentinel")
            private = runtime / "private"
            private.mkdir()
            (private / "submission.pdf").write_bytes(b"private submission sentinel")
            with override_settings(STATIC_ROOT=public), patch(
                "django.core.wsgi.get_wsgi_application", return_value=fallback
            ):
                module = load_config("codespaces_wsgi.py", {"DJANGO_SETTINGS_MODULE": "config.codespaces"})

            def request(path):
                environ = {}
                setup_testing_defaults(environ)
                environ["PATH_INFO"] = unquote(path)
                response = {}

                def start_response(status, headers):
                    response.update(status=status, headers=dict(headers))

                body = module.application(environ, start_response)
                try:
                    response["body"] = b"".join(body)
                finally:
                    if hasattr(body, "close"):
                        body.close()
                return response

            css = request("/static/site.css")
            self.assertEqual(css["status"], "200 OK")
            self.assertEqual(css["body"], b"body { color: navy; }")
            for path in (
                "/school.sqlite3",
                "/private/submission.pdf",
                "/static/../school.sqlite3",
                "/static/%2e%2e/private/submission.pdf",
                "/static/private/submission.pdf",
            ):
                with self.subTest(path=path):
                    response = request(path)
                    self.assertEqual(response["status"], "404 Not Found")
                    self.assertNotIn(b"private", response["body"])

    def test_wsgi_refuses_development_settings(self):
        with self.assertRaises(ImproperlyConfigured):
            load_config("codespaces_wsgi.py", {"DJANGO_SETTINGS_MODULE": "config.settings"})


class CodespacesHostTests(SimpleTestCase):
    canonical = "test-classroom-123456-8000.app.github.dev"

    def wrapper(self, application):
        with TemporaryDirectory() as directory, override_settings(STATIC_ROOT=directory), patch(
            "django.core.wsgi.get_wsgi_application", return_value=application
        ):
            module = load_config("codespaces_wsgi.py", {"DJANGO_SETTINGS_MODULE": "config.codespaces"})
        return module.with_codespaces_host(application, self.canonical)

    def test_only_exact_internal_host_is_normalized_without_changing_origin(self):
        received = []

        def application(environ, start_response):
            received.append(environ.copy())
            return []

        wrapped = self.wrapper(application)
        for host in ("localhost:8000", self.canonical, "evil.test", "localhost.evil.test:8000", "localhost:8001"):
            with self.subTest(host=host):
                environ = {"HTTP_HOST": host, "HTTP_ORIGIN": "https://origin.example.test"}
                wrapped(environ, None)
                self.assertEqual(received[-1]["HTTP_HOST"], self.canonical if host == "localhost:8000" else host)
                self.assertEqual(received[-1]["HTTP_ORIGIN"], "https://origin.example.test")

    def test_forwarded_host_cannot_supply_the_canonical_identity(self):
        received = []

        def application(environ, start_response):
            received.append(environ["HTTP_HOST"])
            return []

        wrapped = self.wrapper(application)
        wrapped({"HTTP_HOST": "localhost:8000", "HTTP_X_FORWARDED_HOST": "evil.test"}, None)
        wrapped({"HTTP_HOST": self.canonical, "HTTP_X_FORWARDED_HOST": "evil.test"}, None)
        wrapped({"HTTP_HOST": "evil.test", "HTTP_X_FORWARDED_HOST": self.canonical}, None)
        self.assertEqual(received, [self.canonical, self.canonical, "evil.test"])

    def test_django_accepts_tunnel_host_and_rejects_other_hosts(self):
        with override_settings(
            DEBUG=False, ALLOWED_HOSTS=[self.canonical],
            SECURE_SSL_REDIRECT=True, SECURE_PROXY_SSL_HEADER=None,
            USE_X_FORWARDED_HOST=False,
        ):
            wrapped = self.wrapper(get_wsgi_application())
            for host, expected in (
                ("localhost:8000", "200 OK"),
                (self.canonical, "200 OK"),
                ("evil.test", "400 Bad Request"),
                ("localhost.evil.test:8000", "400 Bad Request"),
                ("localhost:8001", "400 Bad Request"),
            ):
                with self.subTest(host=host):
                    environ = {}
                    setup_testing_defaults(environ)
                    environ.update(HTTP_HOST=host, HTTP_X_FORWARDED_HOST=self.canonical,
                                   PATH_INFO="/health/", **{"wsgi.url_scheme": "https"})
                    statuses = []
                    body = wrapped(environ, lambda status, headers: statuses.append(status))
                    try:
                        content = b"".join(body)
                    finally:
                        body.close()
                    self.assertEqual(statuses, [expected])
                    if expected == "200 OK":
                        self.assertEqual(content, b'{"status": "ok"}')
