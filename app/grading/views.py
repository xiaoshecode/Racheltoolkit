import hashlib
import json
from pathlib import Path
from uuid import uuid4

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from school.permissions import teacher_classes
from storage.backends import StorageError
from storage.preview_views import preview_file, preview_status
from storage.previews import pdf_manifest
from storage.services import ensure_written, file_response, reserve
from submissions.models import Submission
from .models import GradeDraft, ReleasedGrade
from .services import (
    Conflict,
    Invalid,
    personal_cards,
    personal_grades,
    publish_answers,
    publish_grades,
    save_answers,
    save_grade,
    solution_visible,
    student_task,
    teacher_submission,
    teacher_task,
    whole,
)


def failure(request, error):
    submitted = [
        (key, value)
        for key in request.POST
        if key != "csrfmiddlewaretoken"
        for value in request.POST.getlist(key)
    ]
    return render(
        request,
        "grading/error.html",
        {"error": str(error), "submitted": submitted, "path": request.path},
        status=409 if isinstance(error, Conflict) else 400,
    )


def files_for(sub):
    files = []
    for part in sub.attachments.select_related("file"):
        base = f"/submissions/{sub.pk}/attachments/{part.pk}/"
        name = part.file.original_name
        suffix = Path(name).suffix.lower()
        kind = "image" if suffix in {".jpg", ".jpeg", ".png"} else "pdf" if suffix == ".pdf" else "office"
        files.append(
            dict(
                id=str(part.pk),
                name=name,
                kind=kind,
                sha256=part.file.sha256,
                file_url=base + "download/?inline=1",
                preview_url=base + "preview/",
                status_url=base + "preview-status/",
            )
        )
    return files


def judgment_rows(key, draft):
    if not key:
        return []
    previous = {j["question_id"]: j for j in draft.judgments} if draft else {}
    return [
        {"question": e.answer.question, "old": previous.get(e.answer.question_id, {})}
        for e in key.entries.select_related("answer__question")
    ]


@login_required
@require_http_methods(["GET", "POST"])
def review(request, pk):
    sub = teacher_submission(request.user, pk)
    draft = GradeDraft.objects.filter(submission=sub).first()
    keys = sub.assignment.answer_revisions.order_by("-seq").select_related("paper")
    key = draft.key if draft and draft.key_id else keys.first()
    if request.method == "GET" and request.GET.get("key"):
        try:
            key = get_object_or_404(keys, pk=whole(request.GET["key"]))
        except Invalid as error:
            return failure(request, error)
    if request.method == "POST":
        try:
            key_id = request.POST.get("key_id")
            selected = (
                get_object_or_404(sub.assignment.answer_revisions, pk=whole(key_id)) if key_id else None
            )
            judgments = []
            for row in judgment_rows(selected, None):
                q = row["question"].pk
                verdict = request.POST.get(f"verdict_{q}")
                if verdict:
                    judgments.append(
                        dict(
                            question_id=q,
                            verdict=verdict,
                            reason=request.POST.get(f"reason_{q}", ""),
                            attachment_id=request.POST.get(f"attachment_{q}"),
                            page=request.POST.get(f"page_{q}"),
                        )
                    )
            raw = request.POST.get("annotations")
            if not raw and draft and draft.annotations:
                raise Invalid("页上批注未载入，未保存改动。")
            try:
                annotations = json.loads(raw or "[]")
            except (ValueError, RecursionError):
                raise Invalid("批注数据格式无效，请保留当前输入并重新打开原版本。") from None
            save_grade(
                request.user,
                sub.pk,
                request.POST.get("version"),
                request.POST.get("score"),
                request.POST.get("feedback", ""),
                key_id,
                annotations,
                judgments,
            )
            return redirect("grading:review", pk=pk)
        except Invalid as error:
            return failure(request, error)
    return render(
        request,
        "grading/review.html",
        dict(
            sub=sub,
            draft=draft,
            key=key,
            keys=keys,
            attachment_list=files_for(sub),
            editable=True,
            annotations=draft.annotations if draft else [],
            judgments=judgment_rows(key, draft if draft and draft.key == key else None),
            snapshots=sub.grades.select_related("release").order_by("-release_id"),
            newer=Submission.objects.filter(
                assignment=sub.assignment, student=sub.student, state="confirmed", version__gt=sub.version
            ).exists(),
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def release_grades(request, pk):
    task = teacher_task(request.user, pk)
    if request.method == "POST":
        try:
            choices = {}
            for value in request.POST.getlist("draft"):
                sub_id, sep, version = value.partition(":")
                if not sep or sub_id in choices:
                    raise Invalid("重复或无效草稿。")
                choices[sub_id] = version
            batch = publish_grades(request.user, pk, request.POST.get("request_key"), choices)
            return render(request, "grading/published.html", dict(task=task, batch=batch))
        except Invalid as error:
            return failure(request, error)
    submissions = (
        task.submissions.filter(
            state="confirmed",
            student__is_active=True,
            student__enrollments__teaching_class=task.teaching_class,
            student__enrollments__active=True,
        )
        .select_related("student", "grade_draft")
        .order_by("student__username", "-version")
    )
    latest = {}
    for sub in submissions:
        latest.setdefault(sub.student_id, sub)
    selected = list(latest.values())
    # An older version can still be selected deliberately when the teacher opened it before a resubmission.
    return render(
        request,
        "grading/publish.html",
        dict(
            task=task,
            submissions=submissions,
            ungraded=sum(not hasattr(sub, "grade_draft") for sub in selected),
            pending=sum(hasattr(sub, "grade_draft") for sub in selected),
            request_key=uuid4().hex,
        ),
    )


def accept_pdf(upload):
    if upload.size > 20 * 1024 * 1024 or Path(upload.name).suffix.lower() != ".pdf":
        raise Invalid("附件须为 20 MiB 以内的 PDF。")
    data = upload.read()
    try:
        if not data.startswith(b"%PDF-"):
            raise ValueError()
        pdf_manifest(data)
    except Exception:
        raise Invalid("无法读取答案 PDF。") from None
    record = reserve("answer", upload.name, len(data), hashlib.sha256(data).hexdigest())
    return ensure_written(record, data)


@login_required
@require_http_methods(["GET", "POST"])
def teacher_answers(request, pk):
    task = teacher_task(request.user, pk)
    latest = task.answer_revisions.order_by("-seq").select_related("paper", "file").first()
    if request.method == "POST":
        try:
            if request.POST.get("action") == "publish":
                publish_answers(
                    request.user, pk, request.POST.get("revision"), request.POST.get("request_key")
                )
            elif request.POST.get("action") == "save":
                file = latest.file if latest and not request.POST.get("remove_file") else None
                if upload := request.FILES.get("file"):
                    file = accept_pdf(upload)
                cols = [request.POST.getlist(col) for col in ("number", "prompt", "topic", "answer")]
                if len(set(map(len, cols))) != 1:
                    raise Invalid("题目行不完整。")
                questions = [
                    dict(zip(("number", "prompt", "topic", "answer"), row)) for row in zip(*cols) if any(row)
                ]
                save_answers(
                    request.user,
                    pk,
                    request.POST.get("version"),
                    request.POST.get("paper"),
                    request.POST.get("paper_version"),
                    questions,
                    file,
                )
            else:
                raise Invalid("请选择保存或发布。")
            return redirect("grading:teacher_answers", pk=pk)
        except Invalid as error:
            return failure(request, error)
        except StorageError:
            return HttpResponse("答案文件存储暂时不可用，请重试。", status=503)
    return render(
        request,
        "grading/teacher_answers.html",
        dict(
            task=task,
            key=latest,
            entries=latest.entries.select_related("answer__question") if latest else [],
            published=task.answer_releases.select_related("revision").order_by("-pk").first(),
            request_key=uuid4().hex,
        ),
    )


@login_required
@require_GET
def answers(request, pk):
    task = student_task(request.user, pk)
    release = task.answer_releases.select_related("revision__paper", "revision__file").order_by("-pk").first()
    return render(
        request,
        "grading/answers.html",
        dict(
            task=task,
            release=release,
            entries=release.revision.entries.select_related("answer__question") if release else [],
        ),
    )


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def answer_file(request, pk, revision, action, teacher=False):
    task = teacher_task(request.user, pk) if teacher else student_task(request.user, pk)
    revisions = (
        task.answer_revisions if teacher else task.answer_revisions.filter(answerrelease__assignment=task)
    )
    key = get_object_or_404(revisions.select_related("file"), pk=revision, file__state="ready")
    try:
        if action == "preview-status":
            return preview_status(request, key.file, can_retry=teacher)
        if action == "preview":
            preview_status(request, key.file, can_retry=False)
            return preview_file(request, key.file)
        if request.method not in ("GET", "HEAD"):
            raise Http404
        return file_response(key.file)
    except StorageError:
        return HttpResponse("答案附件暂时不可用。", status=503)


@login_required
@require_GET
def grades(request):
    latest = {}
    for grade in personal_grades(request.user):
        latest.setdefault(grade.release.assignment_id, grade)
    return render(request, "grading/grades.html", {"grades": list(latest.values())})


@login_required
@require_GET
def grade_detail(request, pk, teacher=False):
    grades = (
        ReleasedGrade.objects.filter(release__assignment__teaching_class__in=teacher_classes(request.user))
        if teacher else personal_grades(request.user)
    )
    grade = get_object_or_404(grades, pk=pk)
    visible = teacher or solution_visible(grade)
    return render(
        request,
        "grading/detail.html",
        dict(
            grade=grade,
            visible=visible,
            teacher=teacher,
            editable=False,
            attachment_list=files_for(grade.submission) if visible else [],
            annotations=grade.annotations if visible else [],
        ),
    )


@login_required
@require_GET
def mistakes(request):
    cards = personal_cards(request.user)
    term = request.GET.get("q", "").strip()[:100]
    if term:
        from django.db.models import Q

        cards = cards.filter(
            Q(question__number=term)
            | Q(question__topic__icontains=term)
            | Q(question__paper__code__icontains=term)
            | Q(assignment__teaching_class__course__title__icontains=term)
        )
    if request.GET.get("state") in {"0", "1"}:
        cards = cards.filter(reviewed=request.GET["state"] == "1")
    return render(request, "grading/mistakes.html", dict(cards=cards.order_by("-grade_id", "pk"), q=term))


@login_required
@require_POST
def review_card(request, pk):
    card = get_object_or_404(personal_cards(request.user), pk=pk)
    status = request.POST.get("reviewed")
    if status not in {"0", "1"}:
        return failure(request, Invalid("无效的复习状态。"))
    card.reviewed = status == "1"
    card.save(update_fields=["reviewed"])
    return redirect("grading:mistakes")
