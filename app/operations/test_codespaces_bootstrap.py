from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


class CodespacesBootstrapTests(TestCase):
    def test_first_account_is_private_and_restart_never_resets_password(self):
        with TemporaryDirectory() as directory, override_settings(
            SETTINGS_MODULE="config.codespaces", RUNTIME=Path(directory)
        ):
            output = StringIO()
            call_command("bootstrap_codespace", stdout=output)
            card = Path(directory) / "access.txt"
            contents = card.read_text(encoding="utf-8")
            password = contents.split("密码：")[1].splitlines()[0]
            user = get_user_model().objects.get(username="rachel-admin")
            self.assertTrue(user.is_superuser and user.check_password(password))
            self.assertNotIn(password, output.getvalue())
            call_command("bootstrap_codespace", stdout=output)
            self.assertEqual(card.read_text(encoding="utf-8"), contents)
            self.assertEqual(get_user_model().objects.count(), 1)

    def test_existing_accounts_or_access_card_are_never_overwritten(self):
        with TemporaryDirectory() as directory, override_settings(
            SETTINGS_MODULE="config.codespaces", RUNTIME=Path(directory)
        ):
            card = Path(directory) / "access.txt"
            card.write_text("prior credential", encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("bootstrap_codespace")
            self.assertFalse(get_user_model().objects.exists())
            self.assertEqual(card.read_text(), "prior credential")
            get_user_model().objects.create_user("existing")
            call_command("bootstrap_codespace", stdout=StringIO())
            self.assertEqual(get_user_model().objects.count(), 1)
            self.assertEqual(card.read_text(), "prior credential")

    def test_non_codespaces_settings_refuse_bootstrap(self):
        with override_settings(SETTINGS_MODULE="config.production"), self.assertRaises(CommandError):
            call_command("bootstrap_codespace")
