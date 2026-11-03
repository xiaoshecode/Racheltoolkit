from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from school.roster import check_roster, import_roster


class Command(BaseCommand):
    help = "预检 UTF-8 名册 student_id,name,class_id。添加 --apply 后重新校验并整批导入。"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=Path)
        parser.add_argument("--apply", action="store_true")

    def handle(self, csv_path, apply, **options):
        try:
            contents = csv_path.read_text(encoding="utf-8-sig")
            rows = check_roster(contents)
            if not apply:
                return f"已通过预检，共 {len(rows)} 行。"
            accounts, enrollments = import_roster(contents)
            return f"已导入 {len(rows)} 行，新增 {accounts} 个账号、{enrollments} 条选课关系。"
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error
        except (OSError, UnicodeError) as error:
            raise CommandError("无法读取 UTF-8 名册。") from error
