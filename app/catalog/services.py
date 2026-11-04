import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from pypdf import PdfReader

from school.permissions import editable_courses
from storage.previews import RENDERER, queue_preview
from storage.services import ensure_written, reserve
from .models import CoursePublication, CourseRevision, Material, Section


class StaleEdit(Exception):
    pass


def published_chapter_exists(course_id, key):
    return Section.objects.filter(stable_key=key, revision__coursepublication__course_id=course_id).exists()


def draft_for(course):
    return CourseRevision.objects.get_or_create(
        course=course, published_at=None, defaults={"seq": 1, "title": course.title}
    )[0]


def text_field(value, label, limit, *, required=True):
    value = str(value or "").strip()
    if len(value) > limit or (required and not value):
        raise ValidationError(f"请检查{label}，最多 {limit} 字。")
    return value


def next_position(objects):
    return (objects.aggregate(last=Max("position"))["last"] or 0) + 1


def preview_info(item):
    """Resolve a generated draft preview without changing the published revision."""
    if item.preview_file_id:
        return item.preview_file, "ready" if item.preview_file.state == "ready" else "failed"
    asset = item.original_file.previews.filter(
        renderer_version=RENDERER, source_sha256=item.original_file.sha256
    ).select_related("output_file").first()
    if asset and asset.state == "ready" and asset.output_file_id and asset.output_file.state == "ready":
        return asset.output_file, "ready"
    return None, asset.state if asset else "missing"


def receive_file(upload, *, pdf_only=False):
    if not upload or not 0 < upload.size <= 50 * 1024 * 1024:
        raise ValidationError("请选择不超过 50 MiB 的非空文件。")
    suffix = Path(upload.name).suffix.lower()
    allowed = {".pdf"} if pdf_only else {".pdf", ".ppt", ".pptx", ".doc", ".docx"}
    if suffix not in allowed:
        raise ValidationError("支持 PPT、Word 和 PDF，附加预览必须为 PDF。")
    data = upload.read()
    if suffix == ".pdf":
        try:
            valid = data.startswith(b"%PDF-") and 0 < len(PdfReader(BytesIO(data)).pages) <= 500
        except Exception:
            valid = False
    elif suffix in {".pptx", ".docx"}:
        try:
            with ZipFile(BytesIO(data)) as package:
                entry = "ppt/presentation.xml" if suffix == ".pptx" else "word/document.xml"
                valid = entry in package.namelist() and "[Content_Types].xml" in package.namelist()
        except BadZipFile:
            valid = False
    else:
        valid = data.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
    if not valid:
        raise ValidationError("文件内容与格式不符，或 PDF 不能预览。")
    record = reserve("course", upload.name, len(data), hashlib.sha256(data).hexdigest())
    return ensure_written(record, data)


def publish(draft, user):
    originals = Material.objects.filter(section__revision=draft)
    for item in originals.filter(preview_file=None).select_related("original_file"):
        preview = item.original_file.previews.filter(renderer_version=RENDERER, source_sha256=item.original_file.sha256,
                                                     state="ready", output_file__state="ready").first()
        if preview:
            item.preview_file = preview.output_file
            item.save(update_fields=["preview_file"])
    if originals.filter(~Q(original_file__state="ready") |
                        Q(preview_file__isnull=True) | ~Q(preview_file__state="ready")).exists():
        raise ValidationError("请先为每份材料准备好原件和 PDF 预览，再发布课程。")
    draft.published_by = user
    draft.published_at = timezone.now()
    draft.save(update_fields=["published_by", "published_at"])
    CoursePublication.objects.update_or_create(course=draft.course, defaults={"revision": draft})
    clone = CourseRevision.objects.create(course=draft.course, seq=draft.seq + 1, edit_version=draft.edit_version,
                                          title=draft.title, intro=draft.intro)
    for chapter in draft.sections.prefetch_related("materials"):
        new = Section.objects.create(revision=clone, stable_key=chapter.stable_key, position=chapter.position,
                                     title=chapter.title, body=chapter.body)
        Material.objects.bulk_create([
            Material(section=new, stable_key=m.stable_key, position=m.position, title=m.title,
                     original_file_id=m.original_file_id, preview_file_id=m.preview_file_id)
            for m in chapter.materials.all()
        ])


@transaction.atomic
def edit(course, user, expected, fields, blobs=None):
    get_object_or_404(editable_courses(user), pk=course.pk)
    draft = draft_for(course)
    if draft.edit_version != expected:
        raise StaleEdit
    draft.edit_version += 1
    draft.save(update_fields=["edit_version"])
    action = fields.get("action")
    if action == "publish":
        publish(draft, user)
        return
    if action == "course":
        draft.title = text_field(fields.get("title"), "课程名", 160)
        draft.intro = text_field(fields.get("intro"), "课程介绍", 5000, required=False)
        draft.save(update_fields=["title", "intro"])
        return
    if action == "add_section":
        draft.sections.create(position=next_position(draft.sections), title=text_field(fields.get("title"), "章名", 160),
                              body=text_field(fields.get("body"), "章节正文", 10000, required=False))
        return
    section = get_object_or_404(draft.sections, stable_key=fields.get("section") or None)
    if action == "section":
        section.title = text_field(fields.get("title"), "章名", 160)
        section.body = text_field(fields.get("body"), "章节正文", 10000, required=False)
        section.save(update_fields=["title", "body"])
    elif action in {"up", "down"}:
        others = draft.sections
        neighbor = (others.filter(position__lt=section.position).order_by("-position").first() if action == "up"
                    else others.filter(position__gt=section.position).first())
        if not neighbor:
            raise ValidationError("本章已经位于目录边界。")
        old = section.position
        section.position = next_position(others)
        section.save(update_fields=["position"])
        section.position = neighbor.position
        neighbor.position = old
        neighbor.save(update_fields=["position"])
        section.save(update_fields=["position"])
    elif action == "delete_section":
        section.delete()
    elif action == "remove":
        get_object_or_404(section.materials, stable_key=fields.get("material") or None).delete()
    elif action == "retry_preview":
        item = get_object_or_404(section.materials.select_related("original_file"),
                                 stable_key=fields.get("material") or None)
        if item.preview_file_id or item.original_file.state != "ready":
            raise ValidationError("此资料无需重新生成预览，请刷新页面核对。")
        queue_preview(item.original_file, retry=True)
    elif action == "upload" and blobs:
        original, preview = blobs
        if fields.get("material"):
            entry = get_object_or_404(section.materials, stable_key=fields["material"])
        else:
            entry = Material(section=section, position=next_position(section.materials))
        entry.title = text_field(fields.get("title") or original.original_name, "资料名", 160)
        entry.original_file = original
        entry.preview_file = preview
        entry.save()
    else:
        raise ValidationError("未知的课程编辑操作。")
