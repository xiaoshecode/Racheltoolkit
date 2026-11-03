import csv
import io
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Enrollment, TeachingClass
from .permissions import teacher_classes


@dataclass(frozen=True)
class StudentRow:
    username: str
    name: str
    class_id: int


def check_roster(csv_text, teacher=None):
    """Validate the entire CSV before any mutation. Never change another identity's name."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")), strict=True)
    errors = []
    classes = TeachingClass.objects.all() if teacher is None else teacher_classes(teacher)
    class_ids = set(classes.values_list("id", flat=True))
    rows, identifiers, memberships = [], {}, set()
    try:
        if reader.fieldnames != ["student_id", "name", "class_id"]:
            raise ValidationError("表头须为 student_id,name,class_id。")
        for line, values in enumerate(reader, start=2):
            username = (values.get("student_id") or "").strip()
            name = (values.get("name") or "").strip()
            class_value = (values.get("class_id") or "").strip()
            if None in values or not username or len(username) > 150 or not name or len(name) > 150:
                errors.append(f"第 {line} 行的学号、姓名或列数无效。")
                continue
            try:
                get_user_model()._meta.get_field("username").run_validators(username)
                class_id = int(class_value)
            except (ValidationError, ValueError):
                errors.append(f"第 {line} 行的学号或班级编号无效。")
                continue
            if class_id not in class_ids:
                errors.append(f"第 {line} 行的班级不存在或无权管理。")
            if username in identifiers and identifiers[username] != name:
                errors.append(f"第 {line} 行同一学号对应多个姓名。")
            if (username, class_id) in memberships:
                errors.append(f"第 {line} 行的学号与班级重复。")
            identifiers[username] = name
            memberships.add((username, class_id))
            rows.append(StudentRow(username, name, class_id))
    except csv.Error:
        raise ValidationError("CSV 引号、分隔符或字段长度无效。") from None
    if not rows:
        errors.append("名册没有可用的学生记录。")
    accounts = get_user_model().objects.filter(username__in=identifiers)
    for account in accounts:
        if account.first_name != identifiers[account.username] or account.is_staff or account.is_superuser or (
            account.teaching_assignments.exists() or account.course_editorships.exists()
        ):
            errors.append(f"学号 {account.username} 与已有账号冲突。")
    if errors:
        raise ValidationError(errors)
    return rows


@transaction.atomic
def import_roster(csv_text, teacher=None):
    rows = check_roster(csv_text, teacher)
    accounts_created = enrollments_created = 0
    for row in rows:
        account = get_user_model().objects.filter(username=row.username).first()
        if account is None:
            account = get_user_model().objects.create_user(username=row.username, first_name=row.name)
            accounts_created += 1
        enrollment, created = Enrollment.objects.get_or_create(student=account, teaching_class_id=row.class_id)
        if created:
            enrollments_created += 1
        elif not enrollment.active:
            enrollment.active = True
            enrollment.save(update_fields=["active"])
    return accounts_created, enrollments_created
