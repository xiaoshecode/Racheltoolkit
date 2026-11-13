from django.test import SimpleTestCase


class HealthTests(SimpleTestCase):
    def test_health_is_public_and_has_no_private_payload(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class AssistantCredentialTests(SimpleTestCase):
    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name)

    def test_atomic_private_save_read_replace_and_missing_file(self):
        import os
        import stat
        from .assistant_credentials import read_key, save_key
        self.assertEqual(read_key(self.runtime), "")
        path = save_key(self.runtime, "synthetic-test-key-one")
        self.assertEqual(read_key(self.runtime), "synthetic-test-key-one")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        save_key(self.runtime, "synthetic-test-key-two")
        self.assertEqual(read_key(self.runtime), "synthetic-test-key-two")
        self.assertEqual(len(list(self.runtime.iterdir())), 1)

    def test_environment_priority_and_local_only_file_fallback(self):
        import os
        from unittest.mock import patch
        from .assistant_credentials import local_key, save_key
        save_key(self.runtime, "synthetic-local-key")
        for env, expected in (
            ({"DJANGO_SETTINGS_MODULE": "config.settings"}, "synthetic-local-key"),
            ({"DJANGO_SETTINGS_MODULE": "config.settings", "DEEPSEEK_API_KEY": ""}, "synthetic-local-key"),
            ({"DEEPSEEK_API_KEY": "synthetic-env-key"}, "synthetic-env-key"),
            ({"DJANGO_SETTINGS_MODULE": "config.codespaces"}, ""),
            ({"DJANGO_SETTINGS_MODULE": "config.production_local"}, ""),
            ({"DJANGO_SETTINGS_MODULE": "config.production"}, ""),
        ):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(local_key(self.runtime), expected)

    def test_invalid_keys_never_replace_previous_configuration(self):
        from .assistant_credentials import read_key, save_key
        save_key(self.runtime, "synthetic-existing-key")
        for key in ("", "space key", "key\nsecond", "\ud800", "a" * 1025):
            with self.subTest(length=len(key)), self.assertRaises(ValueError):
                save_key(self.runtime, key)
            self.assertEqual(read_key(self.runtime), "synthetic-existing-key")

    def test_failed_atomic_replace_keeps_old_file_and_cleans_temporary(self):
        from unittest.mock import patch
        from .assistant_credentials import read_key, save_key
        save_key(self.runtime, "synthetic-existing-key")
        with patch("config.assistant_credentials.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                save_key(self.runtime, "synthetic-replacement-key")
        self.assertEqual(read_key(self.runtime), "synthetic-existing-key")
        self.assertEqual(len(list(self.runtime.iterdir())), 1)

    def test_symlink_and_public_permissions_are_rejected(self):
        import os
        from .assistant_credentials import read_key, save_key
        if os.name != "posix":
            self.skipTest("POSIX file ownership and permissions")
        target = self.runtime / "other.key"
        target.write_text("synthetic-other-key")
        path = self.runtime / "deepseek.key"
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            read_key(self.runtime)
        with self.assertRaises(ValueError):
            save_key(self.runtime, "synthetic-new-key")
        path.unlink()
        save_key(self.runtime, "synthetic-private-key")
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_key(self.runtime)

    def test_command_uses_hidden_input_without_printing_key(self):
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command
        from django.test import override_settings
        from .assistant_credentials import read_key
        output = StringIO()
        with override_settings(RUNTIME=self.runtime, SETTINGS_MODULE="config.settings"), patch(
            "operations.management.commands.configure_deepseek.getpass.getpass",
            side_effect=["synthetic-command-key", "synthetic-command-key"],
        ):
            call_command("configure_deepseek", stdout=output)
        self.assertEqual(read_key(self.runtime), "synthetic-command-key")
        self.assertNotIn("synthetic-command-key", output.getvalue())
        self.assertIn(str(self.runtime / "deepseek.key"), output.getvalue())

    def test_command_mismatch_noninteractive_and_production_are_rejected(self):
        import getpass
        from unittest.mock import patch
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.test import override_settings
        for answers in (["synthetic-one", "synthetic-two"], getpass.GetPassWarning("no terminal")):
            with override_settings(RUNTIME=self.runtime, SETTINGS_MODULE="config.settings"), patch(
                "operations.management.commands.configure_deepseek.getpass.getpass", side_effect=answers,
            ), self.assertRaises(CommandError):
                call_command("configure_deepseek")
            self.assertFalse((self.runtime / "deepseek.key").exists())
        with override_settings(SETTINGS_MODULE="config.production"), self.assertRaises(CommandError):
            call_command("configure_deepseek")
