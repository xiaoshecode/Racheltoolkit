from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe, require_http_methods

from school.permissions import editable_courses, student_classes, teacher_classes
from storage.backends import StorageError
from storage.previews import prepare_preview, queue_preview
from storage.services import file_response
from .models import CoursePublication, Material
from .services import StaleEdit, draft_for, edit, receive_file


def publications():
    return CoursePublication.objects.select_related("revision").prefetch_related("revision__sections").order_by(
        "revision__title", "course_id")


@require_safe
def home(request):
    return render(request, "catalog/home.html", {
        "publications": publications(), "can_edit": editable_courses(request.user).exists(),
        "is_teacher": teacher_classes(request.user).exists(),
    })


@require_safe
def public_index(request):
    courses = []
    for pub in publications():
        rev = pub.revision
        courses.append({"title": rev.title, "intro": rev.intro, "url": reverse("catalog_course", args=[pub.course_id]),
                        "published_at": rev.published_at.isoformat(), "chapters": [
                            {"title": chapter.title, "url": reverse("catalog_chapter", args=[pub.course_id, chapter.stable_key])}
                            for chapter in rev.sections.all()
                        ]})
    response = JsonResponse({"courses": courses})
    response["Access-Control-Allow-Origin"] = "*"
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
    return render(request, "catalog/course.html", {
        "revision": revision, "rows": [(s, by_section.get(s.stable_key, [])) for s in chapters],
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
            messages.success(request, "课程已发布。" if request.POST.get("action") == "publish" else "草稿已保存。")
            return redirect("catalog_editor", course_id)
        except StaleEdit:
            error, status = "草稿已被其他编辑更新。请对照最新内容，再重新提交。", 409
        except (ValidationError, ValueError) as exc:
            error, status = ("；".join(exc.messages) if isinstance(exc, ValidationError) else "请检查草稿版本和输入。"), 400
        except StorageError:
            error, status = "存储暂不可用，本次资料未绑定。请重试。", 503
    return render(request, "catalog/edit.html", {"rev": draft_for(course), "error": error,
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
    if not record or record.pk != blob:
        raise Http404
    try:
        return file_response(record, inline=mode == "preview")
    except StorageError:
        return HttpResponse("文件暂时无法读取，请稍后重试。", status=503)
