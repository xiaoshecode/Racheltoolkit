"""Build a small, explicitly local review classroom. Never seed a deployed database."""

import hashlib
import secrets
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from catalog.models import CoursePublication
from catalog.services import draft_for, edit
from grading.services import publish_answers, publish_grades, save_answers, save_grade
from school.models import Course, TeachingClass
from storage.services import ensure_written, reserve
from storage.previews import prepare_preview
from submissions.models import Assignment
from submissions.services import submit
from submissions.validation import Item


def lecture_pdf():
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for text in (
        "Linear functions   |   Graph y = 2x + 1",
        "Exercise   |   Explain the slope and intercept.",
    ):
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 20 Tf 55 710 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


class Command(BaseCommand):
    help = "Prepare a synthetic classroom confined to .local/review and print the local access card location."

    def handle(self, **options):
        runtime = settings.RUNTIME.resolve()
        expected = (settings.BASE_DIR / ".local/review").resolve()
        if (
            runtime != expected
            or Path(settings.DATABASES["default"]["NAME"]).resolve() != runtime / "school.sqlite3"
            or settings.SETTINGS_MODULE != "config.settings"
        ):
            raise CommandError("Use scripts/start-review.ps1 with its isolated review database.")
        from storage.backends import backend_name

        if backend_name() != "local":
            raise CommandError("Review requires local-only storage.")
        call_command("seed_school", verbosity=0)
        usermodel = get_user_model()
        teacher = usermodel.objects.get(username="J")
        a = usermodel.objects.get(username="0001")
        course = Course.objects.get(code="DEMO")
        group = TeachingClass.objects.get(course=course, code="X")
        data = lecture_pdf()
        if not CoursePublication.objects.filter(course=course).exists():
            draft = draft_for(course)
            edit(
                course,
                teacher,
                draft.edit_version,
                {
                    "action": "course",
                    "title": "基础数学 · 函数与图像",
                    "intro": "按章节查找讲义，课后从同一章进入本班练习。",
                },
            )
            draft.refresh_from_db()
            edit(
                course,
                teacher,
                draft.edit_version,
                {
                    "action": "add_section",
                    "title": "第一章　一次函数",
                    "body": "本章学习目标：从图像中读出斜率和截距，用两点确定一次函数。先阅读讲义，再完成本班练习。",
                },
            )
            draft.refresh_from_db()
            first = draft.sections.first()
            record = reserve(
                "course", "一次函数讲义.pdf", len(data), hashlib.sha256(data).hexdigest()
            )
            ensure_written(record, data)
            edit(
                course,
                teacher,
                draft.edit_version,
                {"action": "upload", "section": first.stable_key, "title": "一次函数 · 两页讲义"},
                (record, record),
            )
            draft.refresh_from_db()
            edit(course, teacher, draft.edit_version, {"action": "publish"})
        if not Assignment.objects.filter(teaching_class=group).exists():
            chapter = CoursePublication.objects.get(course=course).revision.sections.first()
            now = timezone.now()
            Assignment.objects.create(
                teaching_class=group,
                chapter_key=chapter.stable_key,
                title="课后练习 · 拍照交作业",
                instructions="先画出 y = 2x + 1，再用文字说明斜率和截距。拍两页照片并确认页序，或上传一份 PDF。",
                published_at=now,
                due_at=now + timedelta(days=7),
                max_points=20,
            )
            old = Assignment.objects.create(
                teaching_class=group,
                chapter_key=chapter.stable_key,
                title="课堂例题 · 已公布批阅",
                instructions="阅读两页讲义并完成第 2 题。",
                published_at=now - timedelta(days=2),
                due_at=now + timedelta(hours=1),
                max_points=20,
            )
            sub, _ = submit(
                old.pk,
                a,
                a,
                False,
                "",
                "review-example",
                [Item("作答.pdf", data, hashlib.sha256(data).hexdigest())],
            )
            att = sub.attachments.get()
            preview = prepare_preview(att.file)
            key = save_answers(
                teacher,
                old.pk,
                0,
                "课堂练习",
                1,
                [
                    {
                        "number": "2",
                        "prompt": "直线与纵轴的交点在哪里？",
                        "answer": "(0, 1)。令 x = 0 得到纵截距 1。",
                    }
                ],
            )
            mark = {
                "type": "text",
                "attachment_id": str(att.pk),
                "page": 2,
                "width": 600,
                "height": 800,
                "rotation": 0,
                "x": 0.25,
                "y": 0.35,
                "source_sha256": att.file.sha256,
                "renderer_version": preview.renderer_version,
                "text": "请再核对纵截距。",
            }
            wrong = {
                "question_id": key.entries.get().answer.question_id,
                "attachment_id": str(att.pk),
                "page": 2,
                "verdict": "wrong",
                "reason": "把斜率当作纵截距。",
            }
            grade = save_grade(
                teacher, sub.pk, 0, "16", "图像绘制清楚，请订正第 2 题。", key.pk, [mark], [wrong]
            )
            publish_grades(teacher, old.pk, "review-grade", {str(sub.pk): grade.edit_version})
            Assignment.objects.filter(pk=old.pk).update(
                due_at=timezone.now() - timedelta(seconds=1)
            )
            publish_answers(teacher, old.pk, key.pk, "review-answer")
        card = runtime / "access.txt"
        if not card.exists():
            lines = ["仅限本机验收的虚构账号", "地址 http://127.0.0.1:8765/login/", ""]
            for code, role in (("J", "吉老师"), ("0001", "学生甲"), ("0002", "学生乙（未交）")):
                u = usermodel.objects.get(username=code)
                password = secrets.token_urlsafe(15)
                u.set_password(password)
                u.save(update_fields=["password"])
                lines.append(f"{role}  |  账号 {code}  |  密码 {password}")
            card.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"本机验收账号卡：{card}"
