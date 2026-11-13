"""Prompt for a local API key without placing it in shell history or output."""
import getpass
import warnings

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from config.assistant_credentials import save_key, validate_key


class Command(BaseCommand):
    help = "Securely paste and persist a local-development DeepSeek API key."

    def handle(self, **options):
        if settings.SETTINGS_MODULE != "config.settings":
            raise CommandError("Use this command only with local config.settings; deployments use private environment secrets.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("请粘贴 DeepSeek API Key（输入不显示），然后按回车：")
                validate_key(key)
                confirmation = getpass.getpass("请再次粘贴相同的 Key 并按回车：")
            if key != confirmation:
                raise CommandError("两次输入不一致，未修改配置。")
            destination = save_key(settings.RUNTIME, key)
        except getpass.GetPassWarning:
            raise CommandError("请在本机交互终端运行，以避免密钥回显。") from None
        except (EOFError, KeyboardInterrupt):
            raise CommandError("已取消，未修改配置。") from None
        except (ValueError, OSError):
            raise CommandError("密钥格式或私有文件权限不正确，未能保存。") from None
        self.stdout.write(self.style.SUCCESS(f"已保存到私有配置：{destination}"))
        self.stdout.write("请重启本机 Django 服务；以后使用同一数据目录启动会自动读取。")
