"""Create the first administrator of an empty Codespaces trial without logging credentials."""
import os
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Create an initial Codespaces administrator only when the user table is empty."

    def handle(self, **options):
        if settings.SETTINGS_MODULE != "config.codespaces":
            raise CommandError("This bootstrap command is only for the Codespaces trial.")
        card = settings.RUNTIME / "access.txt"
        with transaction.atomic():
            if get_user_model().objects.exists():
                self.stdout.write("Existing accounts preserved; no password was changed.")
                return
            if card.exists():
                raise CommandError("An access card already exists without accounts. Review the database before continuing.")
            password = secrets.token_urlsafe(24)
            get_user_model().objects.create_superuser("rachel-admin", password=password, first_name="站点管理员")
            descriptor = os.open(card, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(f"Codespaces 教学试用站初始管理员\n账号：rachel-admin\n密码：{password}\n"
                             "管理入口：/admin/\n此文件仅保存在私有工作区，不要提交到 Git 或分享给学生。\n")
        self.stdout.write("Initial administrator created. Read the private .local/codespaces/access.txt file.")
