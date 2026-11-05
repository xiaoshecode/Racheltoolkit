import importlib.util
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.core.checks import Tags, run_checks
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.test import Client, SimpleTestCase, override_settings
from django.urls import path

from storage.backends import get_backend, library_id


def csrf_probe(request):
    return JsonResponse({"token": get_token(request)})


urlpatterns = [path("csrf-probe/", csrf_probe)]


class ProductionLocalTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(dir=settings.RUNTIME)
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name).resolve()
        self.runtime.chmod(0o700)
        self.environment = {
            "TEACH_HOST": "teaching.example.test",
            "TEACH_DATA_DIR": str(self.runtime),
            "TEACH_STORAGE_BACKEND": "local",
            "DJANGO_SECRET_KEY": "synthetic-only-test-key-" + "0123456789abcdef" * 4,
        }

    def load(self, **changes):
        filename = Path(__file__).with_name("production_local.py")
        spec = importlib.util.spec_from_file_location("config._test_production_local", filename)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, self.environment | changes, clear=True):
            spec.loader.exec_module(module)
        return module

    def overrides(self, **changes):
        configured = self.load(**changes)
        return override_settings(**{
            name: getattr(configured, name)
            for name in dir(configured)
            if name.isupper() and name != "DATABASES"
        })

    def test_requires_explicit_deployment_identity_and_storage(self):
        for name in self.environment:
            with self.subTest(missing=name), self.assertRaises(ImproperlyConfigured):
                self.load(**{name: ""})
        for secret in ("development-only-key", "a" * 64, "django-insecure-" + "0123456789abcdef" * 4):
            with self.subTest(secret=secret), self.assertRaises(ImproperlyConfigured):
                self.load(DJANGO_SECRET_KEY=secret)
        for backend in ("seafile", "unknown"):
            with self.subTest(backend=backend), self.assertRaises(ImproperlyConfigured):
                self.load(TEACH_STORAGE_BACKEND=backend)

    def test_accepts_exact_hosts_and_rejects_ambiguous_authorities(self):
        for host in ("teaching.example.test", "203.0.113.10", "[2001:db8::1]"):
            with self.subTest(host=host):
                configured = self.load(TEACH_HOST=host)
                self.assertEqual(configured.ALLOWED_HOSTS, [host])
                self.assertEqual(configured.CSRF_TRUSTED_ORIGINS, [f"https://{host}"])
        for host in (
            "*", ".example.test", "example.test.", "localhost", "https://example.test",
            "example.test:8000", "example.test/path", "example.test?x=1", "user@example.test",
            "example.test,evil.test", "example.test\nevil.test", "256.1.2.3", "[::1]:8000",
            "[fe80::1%eth0]", "-bad.example.test", "bad_.example.test",
        ):
            with self.subTest(host=host), self.assertRaises(ImproperlyConfigured):
                self.load(TEACH_HOST=host)

    def test_refuses_missing_relative_root_or_unrelated_database(self):
        absent = self.runtime / "missing-volume"
        for directory in (str(absent), "relative-private-data", self.runtime.anchor):
            with self.subTest(directory=directory), self.assertRaises(ImproperlyConfigured):
                self.load(TEACH_DATA_DIR=directory)
        self.assertFalse(absent.exists())
        for database in (str(self.runtime / "elsewhere.sqlite3"), "school.sqlite3"):
            with self.subTest(database=database), self.assertRaises(ImproperlyConfigured):
                self.load(TEACH_DB=database)
        self.load(TEACH_DB=str(self.runtime / "school.sqlite3"))

    @skipUnless(os.name == "posix", "POSIX service ownership and mode checks")
    def test_refuses_nonprivate_data_or_symlinked_public_paths(self):
        self.runtime.chmod(0o755)
        with self.assertRaises(ImproperlyConfigured):
            self.load()
        self.runtime.chmod(0o700)
        private = self.runtime / "private"
        private.mkdir()
        (self.runtime / "staticfiles").symlink_to(private, target_is_directory=True)
        with self.assertRaises(ImproperlyConfigured):
            self.load()

    def test_http_redirects_and_forwarded_host_cannot_override_identity(self):
        with self.overrides():
            response = self.client.get("/health/", HTTP_HOST="teaching.example.test")
            self.assertEqual(response.status_code, 301)
            self.assertEqual(response["Location"], "https://teaching.example.test/health/")
            response = self.client.get("/health/", HTTP_HOST="teaching.example.test", HTTP_X_FORWARDED_PROTO="https")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["X-Frame-Options"], "DENY")
            self.assertIn("max-age=86400", response["Strict-Transport-Security"])
            self.assertEqual(self.client.get(
                "/health/", HTTP_HOST="unrelated.test", HTTP_X_FORWARDED_PROTO="https",
                HTTP_X_FORWARDED_HOST="teaching.example.test",
            ).status_code, 400)
            for private_url in ("/school.sqlite3", "/private/storage/test.pdf"):
                self.assertEqual(self.client.get(
                    private_url, secure=True, HTTP_HOST="teaching.example.test",
                ).status_code, 404)

    def test_csrf_still_requires_token_and_exact_https_origin(self):
        with self.overrides(), override_settings(ROOT_URLCONF=__name__):
            client = Client(enforce_csrf_checks=True)
            host = "teaching.example.test"
            response = client.get("/csrf-probe/", secure=True, HTTP_HOST=host)
            token = response.json()["token"]
            self.assertTrue(response.cookies["csrftoken"]["secure"])
            for origin, body, expected in (
                (f"https://{host}", {"csrfmiddlewaretoken": token}, 200),
                ("https://unrelated.test", {"csrfmiddlewaretoken": token}, 403),
                (f"http://{host}", {"csrfmiddlewaretoken": token}, 403),
                (f"https://{host}", {}, 403),
            ):
                with self.subTest(origin=origin, has_token=bool(body)):
                    self.assertEqual(client.post(
                        "/csrf-probe/", body, secure=True, HTTP_HOST=host, HTTP_ORIGIN=origin,
                    ).status_code, expected)

    def test_restored_library_and_private_objects_survive_new_backend_instances(self):
        (self.runtime / "restored-library.txt").write_text("preserved-library", encoding="utf-8")
        object_path = "/学生作业/" + "a" * 32 + ".pdf"
        with self.overrides():
            get_backend().put(object_path, b"private migrated lesson")
            self.assertEqual(library_id(), "preserved-library")
        with self.overrides():
            with get_backend().open(object_path) as source:
                self.assertEqual(source.read(), b"private migrated lesson")
            self.assertEqual(library_id(), "preserved-library")
            self.assertEqual(self.load().DATABASES["default"]["NAME"], self.runtime / "school.sqlite3")
            self.assertEqual(settings.LOCAL_STORAGE_ROOT, self.runtime / "private/storage")
            self.assertFalse((settings.STATIC_ROOT / "学生作业").exists())

    def test_collectstatic_is_readable_without_relaxing_private_storage(self):
        source = self.runtime / "static-source"
        (source / "styles").mkdir(parents=True)
        (source / "styles/course.css").write_text("body { color: navy; }", encoding="utf-8")
        private_path = "/学生作业/" + "b" * 32 + ".pdf"
        with self.overrides(), override_settings(
            STATICFILES_DIRS=[source],
            STATICFILES_FINDERS=["django.contrib.staticfiles.finders.FileSystemFinder"],
        ):
            backend = get_backend()
            backend.put(private_path, b"private student work")
            call_command("collectstatic", interactive=False, verbosity=0)
            css = settings.STATIC_ROOT / "styles/course.css"
            self.assertEqual(css.read_text(encoding="utf-8"), "body { color: navy; }")
            self.assertFalse((settings.STATIC_ROOT / "学生作业").exists())
            with backend.open(private_path) as original:
                self.assertEqual(original.read(), b"private student work")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(css.stat().st_mode), 0o644)
                self.assertEqual(stat.S_IMODE(css.parent.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(settings.STATIC_ROOT.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(backend.filename(private_path).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)

    def test_deployment_security_checks_pass_without_broad_suppression(self):
        with self.overrides():
            messages = run_checks(tags=[Tags.security], include_deployment_checks=True)
            self.assertEqual([message for message in messages if not message.is_silenced()], [])
            self.assertEqual(set(settings.SILENCED_SYSTEM_CHECKS), {"security.W005", "security.W021"})
