from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from school.accounts import issue_code
from school.models import ActivationOrReset


class Command(BaseCommand):
    help = "为已核验身份的单个用户签发一小时有效的凭证。将终端显示的凭证私下交给本人。"

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("purpose", choices=dict(ActivationOrReset.PURPOSES))

    def handle(self, username, purpose, **options):
        try:
            user = get_user_model().objects.get(username=username)
            return issue_code(user, purpose)
        except (get_user_model().DoesNotExist, ValidationError) as error:
            raise CommandError("账号或用途无效，无法签发凭证。") from error
