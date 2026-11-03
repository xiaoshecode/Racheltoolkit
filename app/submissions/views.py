from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.db import OperationalError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from school.permissions import student_classes, teacher_classes
from storage.backends import StorageError
from storage.services import file_response
from . import services
from .forms import AssignmentForm, ExtensionForm
from .models import Assignment, Submission
from .validation import Refused, validate


def visible(user):
    return Assignment.objects.filter(
        Q(teaching_class__in=teacher_classes(user))
        | Q(teaching_class__in=student_classes(user), published_at__lte=timezone.now())
    ).select_related("teaching_class")


def receipts(user):
    return (
        Submission.objects.filter(state="confirmed")
        .filter(
            Q(student=user, assignment__teaching_class__in=student_classes(user))
            | Q(assignment__teaching_class__in=teacher_classes(user))
        )
        .select_related("student", "submitted_by", "assignment")
    )


def output(sub):
    result = {"id": sub.pk, "state": sub.state, "version": sub.version}
    if sub.state == "confirmed":
        result.update(
            receipt=str(sub.pk),
            received_at=sub.received_at,
            student=sub.student.username,
            submitted_by=sub.submitted_by.username if sub.submitted_by_id else None,
            source=sub.source,
            note=sub.note,
            url=f"/submissions/{sub.pk}/",
            attachments=[
                {
                    "id": a.pk,
                    "position": a.position,
                    "name": a.file.original_name,
                    "size": a.file.size,
                    "sha256": a.file.sha256,
                    "url": f"/submissions/{sub.pk}/attachments/{a.pk}/download/",
                }
                for a in services.attachments(sub)
            ],
        )
    return result


@login_required
def index(request):
    return render(
        request,
        "submissions/index.html",
        {
            "assignments": visible(request.user).order_by("-due_at"),
            "classes": teacher_classes(request.user),
        },
    )


@login_required
def detail(request, pk):
    work = get_object_or_404(visible(request.user), pk=pk)
    teacher = teacher_classes(request.user).filter(pk=work.teaching_class_id).exists()
    submitted = receipts(request.user).filter(assignment=work).order_by("-version")
    roster = []
    if teacher:
        current = {}
        for sub in submitted:
            current.setdefault(sub.student_id, sub)
        roster = [
            (m.student, current.get(m.student_id))
            for m in work.teaching_class.enrollments.filter(active=True, student__is_active=True)
            .select_related("student")
            .order_by("student__username")
        ]
    return render(
        request,
        "submissions/detail.html",
        {
            "work": work,
            "teacher": teacher,
            "roster": roster,
            "mine": submitted.filter(student=request.user),
            "key": uuid4(),
            "extension": ExtensionForm(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def create(request, class_id):
    group = get_object_or_404(teacher_classes(request.user), pk=class_id)
    form = AssignmentForm(
        request.POST or None, initial={"chapter_key": request.GET.get("chapter_key", "")}
    )
    if request.method == "POST" and form.is_valid():
        work = form.save(commit=False)
        work.teaching_class = group
        work.published_at = timezone.now()
        if work.due_at <= work.published_at:
            form.add_error("due_at", "截止时间须晚于现在。")
        elif work.chapter_key and not _chapter_in_course(group, work.chapter_key):
            form.add_error("chapter_key", "请选择本课程已发布的章节编号。")
        else:
            work.save()
            return redirect("assignment", pk=work.pk)
    return render(request, "submissions/create.html", {"group": group, "form": form})


def _chapter_in_course(group, key):
    from catalog.services import published_chapter_exists

    return published_chapter_exists(group.course_id, key)


@login_required
@require_POST
def extension(request, pk):
    get_object_or_404(Assignment, pk=pk, teaching_class__in=teacher_classes(request.user))
    form = ExtensionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": "延期时间或说明无效。"}, status=400)
    try:
        services.extend(pk, request.user, form.cleaned_data["until"], form.cleaned_data["reason"])
    except Refused as e:
        return JsonResponse({"error": str(e)}, status=e.status)
    return redirect("assignment", pk=pk)


@login_required
@require_POST
def upload(request, pk, student_id=None):
    work = get_object_or_404(visible(request.user), pk=pk)
    student = (
        request.user if student_id is None else get_object_or_404(get_user_model(), pk=student_id)
    )
    behalf = student_id is not None
    services.authorize(work, student, request.user, behalf)
    if any(
        k in request.POST
        for k in ("source", "student", "student_id", "submitted_by", "submitted_by_id")
    ):
        return JsonResponse({"error": "身份由会话和任教关系确定。"}, status=400)
    files = request.FILES.getlist("files")
    single = request.FILES.getlist("file")
    if single and (files or len(single) != 1):
        return JsonResponse({"error": "请使用一种附件字段。"}, status=400)
    try:
        items = validate(files or single)
        sub, status = services.submit(
            pk,
            student,
            request.user,
            behalf,
            request.POST.get("note", "") if behalf else "",
            request.POST.get("key", ""),
            items,
        )
    except Refused as e:
        return JsonResponse({"error": str(e)}, status=e.status)
    except OperationalError as e:
        if "locked" not in str(e).lower():
            raise
        retry = JsonResponse({"error": "收件繁忙，请保留本次编号并重试。"}, status=503)
        retry["Retry-After"] = "2"
        return retry
    return JsonResponse(output(sub), status=status)


@login_required
def status(request, pk, student_id=None):
    work = get_object_or_404(visible(request.user), pk=pk)
    student = (
        request.user if student_id is None else get_object_or_404(get_user_model(), pk=student_id)
    )
    services.authorize(work, student, request.user, student_id is not None)
    sub = get_object_or_404(
        Submission, assignment=work, student=student, retry_key=request.GET.get("key", "")
    )
    code = 200 if sub.state == "confirmed" else (409 if sub.state == "quarantined" else 202)
    return JsonResponse(output(sub), status=code)


@login_required
def receipt(request, pk):
    sub = get_object_or_404(receipts(request.user), pk=pk)
    return render(
        request, "submissions/receipt.html", {"sub": sub, "attachments": services.attachments(sub)}
    )


@login_required
def attachment(request, pk, aid=None, action="download"):
    sub = get_object_or_404(receipts(request.user), pk=pk)
    if aid is None:
        parts = list(services.attachments(sub))
        if len(parts) != 1:
            return receipt(request, pk)
        item = parts[0]
    else:
        item = get_object_or_404(services.attachments(sub), pk=aid)
    try:
        if action == "preview-status":
            from storage.preview_views import preview_status

            return preview_status(
                request,
                item.file,
                teacher_classes(request.user).filter(pk=sub.assignment.teaching_class_id).exists(),
            )
        if request.method != "GET":
            return JsonResponse({"error": "需要 GET 请求。"}, status=405)
        if action == "preview":
            from storage.preview_views import preview_file

            return preview_file(request, item.file)
        return file_response(item.file, inline=request.GET.get("inline") == "1")
    except StorageError:
        return JsonResponse({"error": "文件服务暂不可用，请重试。"}, status=503)
