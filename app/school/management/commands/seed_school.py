from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from school.models import Course, CourseEditor, Enrollment, TeachingAssignment, TeachingClass, Term


class Command(BaseCommand):
    help = "仅在本地或测试环境建立虚构两班，不设置通用密码。"

    @transaction.atomic
    def handle(self, **options):
        if getattr(settings, "DEPLOYED", False) or settings.SETTINGS_MODULE.endswith("production"):
            raise CommandError("生产配置不能生成试验用户。")
        term, _ = Term.objects.get_or_create(code="演练学期")
        course, _ = Course.objects.get_or_create(code="DEMO", defaults={"title": "基础数学"})
        classes = {}
        for code in ("X", "Y"):
            classes[code], _ = TeachingClass.objects.get_or_create(term=term, course=course, code=code)
        for username, name, code, teacher, active in [
            ("J", "吉老师", "X", True, True), ("K", "乙老师", "Y", True, True),
            ("0001", "林同学", "X", False, True), ("0002", "林同学", "X", False, True),
            ("0003", "林同学", "Y", False, True), ("0004", "已退班", "X", False, False),
        ]:
            user = get_user_model().objects.filter(username=username).first()
            if user is None:
                user = get_user_model().objects.create_user(username=username, first_name=name)
            elif user.first_name != name:
                raise CommandError("现有账号与演练身份冲突，已取消。")
            if teacher:
                TeachingAssignment.objects.get_or_create(teaching_class=classes[code], teacher=user)
            else:
                Enrollment.objects.get_or_create(teaching_class=classes[code], student=user, defaults={"active": active})
            if username == "J":
                CourseEditor.objects.get_or_create(course=course, teacher=user)
        return "虚构课程 DEMO，班级 X/Y，教师 J/K，学生 0001–0004 已就绪。"
