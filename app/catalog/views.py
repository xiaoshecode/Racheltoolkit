from pathlib import Path
from urllib.parse import urlencode

from django.contrib import messages
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_safe, require_http_methods

from school.permissions import editable_courses, student_classes, teacher_classes
from storage.backends import StorageError
from storage.previews import prepare_preview, queue_preview
from storage.services import file_response
from .models import CoursePublication, Material
from .services import StaleEdit, draft_for, edit, preview_info, receive_file


def publications():
    return CoursePublication.objects.select_related("revision").prefetch_related(
        "revision__sections__materials__original_file").order_by("revision__title", "course_id")


FORMATS = ("PPTX", "PPT", "PDF", "DOCX", "DOC")


def search_options(request):
    query = request.GET.get("q", "").strip()[:160]
    file_format = request.GET.get("format", "").strip().upper()[:8]
    return {"query": query, "file_format": file_format, "formats": FORMATS,
            "searching": bool(query or file_format), "querystring": urlencode(
                {k:v for k,v in (("q",query), ("format",file_format)) if v})}


def find_sections(revision, chapters, options):
    needle = options["query"].casefold()
    course_match = needle in (revision.title + " " + revision.intro).casefold()
    rows = []
    for chapter in chapters:
        chapter_match = course_match or needle in (chapter.title + " " + chapter.body).casefold()
        files = [item for item in chapter.materials.all()
                 if (not options["file_format"] or item.format == options["file_format"])
                 and (chapter_match or needle in (item.title + " " + item.original_file.original_name).casefold())]
        if files or (chapter_match and not options["file_format"]):
            rows.append((chapter, files))
    return rows


def find_courses(options):
    items = []
    for pub in publications():
        rows = find_sections(pub.revision, pub.revision.sections.all(), options)
        matches_course = options["query"].casefold() in (pub.revision.title + " " + pub.revision.intro).casefold()
        if rows or (matches_course and not options["file_format"]):
            items.append((pub, rows))
    return items


@require_safe
def home(request):
    options = search_options(request)
    return render(request, "catalog/home.html", {
        **options, "publications": find_courses(options), "can_edit": editable_courses(request.user).exists(),
        "is_teacher": teacher_classes(request.user).exists(),
    })


@require_safe
def public_index(request):
    courses = []
    options = search_options(request)
    for pub, sections in find_courses(options):
        rev = pub.revision
        courses.append({"title": rev.title, "intro": rev.intro, "url": reverse("catalog_course", args=[pub.course_id]),
                        "published_at": rev.published_at.isoformat(), "chapters": [
                            {"title": chapter.title, "url": reverse("catalog_chapter", args=[pub.course_id, chapter.stable_key]),
                             "materials": [{"title": m.title, "format": m.format, "size": m.original_file.size} for m in files]}
                            for chapter, files in sections
                        ]})
    response = JsonResponse({"courses": courses})
    if request.headers.get("Origin") == settings.PUBLIC_PORTAL_ORIGIN:
        response["Access-Control-Allow-Origin"] = settings.PUBLIC_PORTAL_ORIGIN
    patch_vary_headers(response, ["Origin"])
    response["Cache-Control"] = "no-cache"
    return response


def course_page(request, revision, chapter_key=None, draft=False):
    chapters = revision.sections.prefetch_related("materials__original_file", "materials__preview_file")
    if chapter_key:
        chapters = [get_object_or_404(chapters, stable_key=chapter_key)]
    teacher_groups = teacher_classes(request.user).filter(course_id=revision.course_id)
    groups = (student_classes(request.user) | teacher_groups).filter(course_id=revision.course_id)
    assignments = []
    if request.user.is_authenticated and not draft:
        from submissions.models import Assignment
        assignments = Assignment.objects.filter(teaching_class__in=groups, published_at__lte=timezone.now(),
                                                chapter_key__in=[s.stable_key for s in chapters]).order_by("due_at", "pk")
    by_section = {}
    for assignment in assignments:
        by_section.setdefault(assignment.chapter_key, []).append(assignment)
    options = search_options(request)
    rows = find_sections(revision, chapters, options)
    if draft:
        for _, files in rows:
            for item in files:
                item.preview_file, _ = preview_info(item)
    return render(request, "catalog/course.html", {
        **options, "revision": revision, "rows": [
            (s, files, by_section.get(s.stable_key, [])) for s, files in rows],
        "teacher_groups": teacher_groups, "draft": draft,
        "file_route": "catalog_draft_file" if draft else "catalog_file",
    })


@require_safe
def course(request, course_id, chapter_key=None):
    return course_page(request, get_object_or_404(publications(), course_id=course_id).revision, chapter_key)


@login_required
@require_safe
def teacher_courses(request):
    courses = editable_courses(request.user)
    if not courses.exists():
        raise Http404
    return render(request, "catalog/teacher.html", {"courses": courses})


@login_required
@require_safe
def draft_preview(request, course_id):
    return course_page(request, draft_for(get_object_or_404(editable_courses(request.user), pk=course_id)), draft=True)


@login_required
@require_http_methods(["GET", "POST"])
def editor(request, course_id):
    course = get_object_or_404(editable_courses(request.user), pk=course_id)
    error, status = "", 200
    if request.method == "POST":
        try:
            expected = int(request.POST.get("edit_version", ""))
            blobs = None
            if request.POST.get("action") == "upload":
                draft = draft_for(course)
                if expected != draft.edit_version:
                    raise StaleEdit
                get_object_or_404(draft.sections, stable_key=request.POST.get("section") or None)
                original = receive_file(request.FILES.get("original"))
                companion = request.FILES.get("preview")
                if companion or Path(original.original_name).suffix.lower() == ".pdf":
                    source = receive_file(companion, pdf_only=True) if companion else original
                    asset = prepare_preview(source)
                    if asset.state != "ready":
                        raise ValidationError("PDF 预览未就绪，请检查附件后重新上传。")
                    preview = asset.output_file
                else:
                    asset = queue_preview(original)
                    preview = asset.output_file if asset.state == "ready" else None
                blobs = (original, preview)
            edit(course, request.user, expected, request.POST, blobs)
            notices = {"publish": "课程已发布。", "retry_preview": "已请求重新生成预览，稍后刷新页面查看结果。"}
            messages.success(request, notices.get(request.POST.get("action"), "草稿已保存。"))
            return redirect("catalog_editor", course_id)
        except StaleEdit:
            error, status = "草稿已被其他编辑更新。请对照最新内容，再重新提交。", 409
        except (ValidationError, ValueError) as exc:
            error, status = ("；".join(exc.messages) if isinstance(exc, ValidationError) else "请检查草稿版本和输入。"), 400
        except StorageError:
            error, status = "存储暂不可用，本次资料未绑定。请重试。", 503
    revision = draft_for(course)
    sections = list(revision.sections.prefetch_related("materials__original_file", "materials__preview_file"))
    for section in sections:
        for item in section.materials.all():
            item.preview_file, item.preview_state = preview_info(item)
    return render(request, "catalog/edit.html", {"rev": revision, "sections": sections, "error": error,
        "submitted": request.POST if status != 200 else {}}, status=status)


@require_safe
def serve(request, course_id, key, blob, mode, draft=False):
    if mode not in {"preview", "download"}:
        raise Http404
    if draft:
        if not request.user.is_authenticated:
            raise Http404
        revision = draft_for(get_object_or_404(editable_courses(request.user), pk=course_id))
    else:
        revision = get_object_or_404(CoursePublication, course_id=course_id).revision
    item = get_object_or_404(Material.objects.select_related("original_file", "preview_file"),
                             section__revision=revision, stable_key=key)
    record = item.preview_file if mode == "preview" else item.original_file
    if draft and mode == "preview" and not record:
        record, _ = preview_info(item)
    if not record or record.pk != blob:
        raise Http404
    if mode == "preview" and request.GET.get("reader") == "1":
        route = "catalog_draft_file" if draft else "catalog_file"
        return render(request, "catalog/reader.html", {
            "item": item, "draft": draft, "source_url": request.path,
            "back_url": reverse("catalog_preview", args=[course_id]) if draft else reverse(
                "catalog_chapter", args=[course_id, item.section.stable_key]),
            "download_url": reverse(route, args=[course_id, key, item.original_file_id, "download"]),
        })
    try:
        return file_response(record, inline=mode == "preview")
    except StorageError:
        return HttpResponse("文件暂时无法读取，请稍后重试。", status=503)
