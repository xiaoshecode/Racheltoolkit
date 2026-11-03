import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from school.models import Enrollment
from school.permissions import student_classes, teacher_classes
from storage.models import PreviewAsset
from storage.previews import RENDERER
from submissions.models import Assignment, Submission
from .models import (
    AnswerKeyRevision,
    AnswerRelease,
    AnswerRevision,
    GradeDraft,
    GradeRelease,
    KeyEntry,
    MistakeCard,
    PaperVersion,
    Question,
    ReleasedGrade,
    ScoreChange,
)


class Invalid(ValueError):
    pass


class Conflict(Invalid):
    pass


def teacher_task(user, pk):
    return get_object_or_404(
        Assignment.objects.select_related("teaching_class__course"),
        pk=pk,
        teaching_class__in=teacher_classes(user),
    )


def student_task(user, pk):
    return get_object_or_404(
        Assignment, pk=pk, published_at__lte=timezone.now(), teaching_class__in=student_classes(user)
    )


def teacher_submission(user, pk):
    return get_object_or_404(
        Submission.objects.select_related("assignment__teaching_class", "student"),
        pk=pk,
        state="confirmed",
        assignment__teaching_class__in=teacher_classes(user),
    )


def bounded_text(value, maximum, *, required=True):
    if not isinstance(value, str) or len(value.strip()) > maximum or required and not value.strip():
        raise Invalid("文本为空或超过长度限制。")
    return value.strip()


def whole(value, minimum=1):
    if (
        type(value) is bool
        or not str(value).isascii()
        or not str(value).isdigit()
        or len(str(value)) > 9
        or int(value) < minimum
    ):
        raise Invalid("编号或版本无效。")
    return int(value)


@transaction.atomic
def save_answers(user, assignment_id, expected, paper_code, paper_version, questions, file=None):
    task = teacher_task(user, assignment_id)
    seq = whole(expected, 0)
    if (task.answer_revisions.order_by("-seq").values_list("seq", flat=True).first() or 0) != seq:
        raise Conflict("答案草稿已更新，请重新打开。")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 200:
        raise Invalid("试卷须包含 1–200 道题。")
    if file and (file.kind != "answer" or file.state != "ready"):
        raise Conflict("答案附件尚未就绪。")
    code = bounded_text(paper_code, 80)
    paper, created = PaperVersion.objects.get_or_create(
        course=task.teaching_class.course, code=code, version=whole(paper_version)
    )
    key = AnswerKeyRevision.objects.create(assignment=task, seq=seq + 1, paper=paper, author=user, file=file)
    numbers = set()
    for position, row in enumerate(questions, 1):
        if not isinstance(row, dict):
            raise Invalid("题目行格式无效。")
        number = bounded_text(row.get("number"), 30)
        prompt = bounded_text(row.get("prompt"), 4000)
        topic = bounded_text(row.get("topic", ""), 100, required=False)
        solution = bounded_text(row.get("answer"), 8000)
        if number in numbers:
            raise Invalid("同卷题号不可重复。")
        numbers.add(number)
        if created:
            question = Question.objects.create(paper=paper, number=number, prompt=prompt, topic=topic)
        else:
            question = paper.questions.filter(number=number, prompt=prompt, topic=topic).first()
            if question is None:
                raise Conflict("题目发生变化，请使用新的试卷版本号。")
        answer = AnswerRevision.objects.create(question=question, text=solution)
        KeyEntry.objects.create(key=key, answer=answer, position=position)
    if paper.questions.count() != len(numbers):
        raise Conflict("原试卷的题目不能删减，请建立新试卷版本。")
    return key


@transaction.atomic
def publish_answers(user, assignment_id, revision_id, request_key):
    task = teacher_task(user, assignment_id)
    retry = bounded_text(request_key, 100)
    revision = get_object_or_404(task.answer_revisions, pk=whole(revision_id))
    prior = task.answer_releases.filter(request_key=retry).first()
    if prior and prior.revision_id != revision.pk:
        raise Conflict("同一发布键不能更改答案版本。")
    if prior:
        return prior
    previous = task.answer_releases.filter(revision=revision).first()
    if previous:
        return previous
    now = timezone.now()
    if now < task.due_at:
        raise Conflict("本班尚未截止。")
    if (
        task.answer_revisions.order_by("-seq").first() != revision
        or revision.file_id
        and revision.file.state != "ready"
    ):
        raise Conflict("答案版本已更新或附件尚未就绪。")
    return AnswerRelease.objects.create(
        assignment=task,
        revision=revision,
        publisher=user,
        due_at=task.due_at,
        released_at=now,
        request_key=retry,
    )


def _unit(n):
    return type(n) in (float, int) and 0 <= n <= 1 and math.isfinite(n)


def validate_layers(submission, key, annotations, judgments):
    if (
        not isinstance(annotations, list)
        or len(annotations) > 600
        or not isinstance(judgments, list)
        or len(judgments) > 200
    ):
        raise Invalid("批注或逐题判断的数量无效。")
    files = {str(item.pk): item.file for item in submission.attachments.select_related("file")}
    pages = {}
    for asset in PreviewAsset.objects.filter(
        source_file__in=files.values(), renderer_version=RENDERER, state="ready"
    ):
        if asset.source_sha256 == asset.source_file.sha256:
            pages[asset.source_file_id] = {p["page"]: p for p in asset.page_manifest}

    def anchor(row):
        if not isinstance(row, dict):
            raise Invalid("作答锚点无效。")
        file = files.get(row["attachment_id"]) if isinstance(row.get("attachment_id"), str) else None
        page = whole(row.get("page"))
        geometry = pages.get(file.pk, {}).get(page) if file else None
        if not geometry:
            raise Invalid("附件或页码不属于这个已就绪的提交版本。")
        return file, geometry

    validated = []
    for row in annotations:
        file, geometry = anchor(row)
        if (
            row.get("source_sha256") != file.sha256
            or row.get("renderer_version") != RENDERER
            or any(row.get(k) != geometry[k] for k in ("width", "height", "rotation"))
            or not _unit(row.get("x"))
            or not _unit(row.get("y"))
        ):
            raise Invalid("批注页的摘要、尺寸或位置不符。")
        mark = {
            k: row[k]
            for k in (
                "type",
                "attachment_id",
                "source_sha256",
                "renderer_version",
                "page",
                "width",
                "height",
                "rotation",
                "x",
                "y",
            )
            if k in row
        }
        mark.update(page=geometry["page"], width=geometry["width"], height=geometry["height"], rotation=geometry["rotation"])
        if row.get("type") == "text":
            mark["text"] = bounded_text(row.get("text"), 500)
        elif row.get("type") == "ink":
            points = row.get("points")
            if (
                not isinstance(points, list)
                or not 1 <= len(points) <= 10000
                or not all(isinstance(p, list) and len(p) == 2 and all(_unit(n) for n in p) for p in points)
                or not _unit(row.get("stroke_width"))
                or not 0 < row["stroke_width"] <= 0.1
            ):
                raise Invalid("手写笔迹点列或笔宽无效。")
            mark.update(points=points, stroke_width=row["stroke_width"])
        else:
            raise Invalid("未知批注类型。")
        validated.append(mark)
    answers = (
        {entry.answer.question_id: entry.answer_id for entry in key.entries.select_related("answer")}
        if key
        else {}
    )
    reports = []
    seen = set()
    for row in judgments:
        _, geometry = anchor(row)
        question_id = whole(row.get("question_id"))
        verdict = row.get("verdict")
        if question_id not in answers or question_id in seen or verdict not in ("wrong", "correct"):
            raise Invalid("逐题判断必须匹配本作业试卷上的唯一题目。")
        seen.add(question_id)
        reason = bounded_text(row.get("reason", ""), 400, required=verdict == "wrong")
        reports.append(
            dict(
                question_id=question_id,
                answer_id=answers[question_id],
                attachment_id=str(row["attachment_id"]),
                page=geometry["page"],
                reason=reason,
                verdict=verdict,
            )
        )
    return validated, reports


@transaction.atomic
def save_grade(user, submission_id, expected, score, feedback, key_id, annotations, judgments):
    sub = teacher_submission(user, submission_id)
    draft = GradeDraft.objects.filter(submission=sub).first()
    if whole(expected, 0) != (draft.edit_version if draft else 0):
        raise Conflict("批阅草稿已更新，请重新打开。")
    try:
        score = Decimal(str(score))
        if (
            not score.is_finite()
            or not 0 <= score <= sub.assignment.max_points
            or score != score.quantize(Decimal("0.01"))
        ):
            raise Invalid("分数须在 0 与满分之间，最多两位小数。")
    except (InvalidOperation, ValueError):
        raise Invalid("分数无效。") from None
    key = get_object_or_404(sub.assignment.answer_revisions, pk=whole(key_id)) if key_id else None
    layers, reports = validate_layers(sub, key, annotations, judgments)
    if draft and draft.score != score:
        ScoreChange.objects.create(draft=draft, actor=user, previous=draft.score, updated=score)
    draft, _ = GradeDraft.objects.update_or_create(
        submission=sub,
        defaults=dict(
            grader=user,
            edit_version=whole(expected, 0) + 1,
            score=score,
            feedback=bounded_text(feedback, 2000, required=False),
            key=key,
            annotations=layers,
            judgments=reports,
        ),
    )
    return draft


@transaction.atomic
def publish_grades(user, assignment_id, request_key, selections):
    task = teacher_task(user, assignment_id)
    retry = bounded_text(request_key, 100)
    if not isinstance(selections, dict) or not 1 <= len(selections) <= 500:
        raise Invalid("请选择本批要发布的学生草稿。")
    try:
        choices = {str(UUID(str(k))): whole(v) for k, v in selections.items()}
    except ValueError:
        raise Invalid("提交编号或草稿版本无效。") from None
    signature = hashlib.sha256(json.dumps(choices, sort_keys=True).encode()).hexdigest()
    batch = GradeRelease.objects.filter(assignment=task, request_key=retry).first()
    if batch:
        if batch.fingerprint != signature:
            raise Conflict("同一发布键对应不同的草稿集合。")
        return batch
    students = set()
    batch = GradeRelease.objects.create(
        assignment=task, publisher=user, request_key=retry, fingerprint=signature
    )
    for sub_id, version in choices.items():
        draft = (
            GradeDraft.objects.filter(
                submission_id=sub_id,
                submission__assignment=task,
                submission__state="confirmed",
                edit_version=version,
            )
            .select_related("submission", "key")
            .first()
        )
        if not draft:
            raise Conflict("所选草稿已改变，重新预览后再发布。")
        sub = draft.submission
        if (
            sub.student_id in students
            or not Enrollment.objects.filter(
                teaching_class=task.teaching_class,
                student_id=sub.student_id,
                student__is_active=True,
                active=True,
            ).exists()
        ):
            raise Conflict("名单有变化或同一学生被选中多次。")
        if ReleasedGrade.objects.filter(
            release__assignment=task, student_id=sub.student_id, submission__version__gt=sub.version
        ).exists():
            raise Conflict("该生已有更新提交版本的已发布成绩。")
        students.add(sub.student_id)
        grade = ReleasedGrade.objects.create(
            release=batch,
            student_id=sub.student_id,
            submission=sub,
            draft_version=version,
            score=draft.score,
            max_points=task.max_points,
            feedback=draft.feedback,
            key=draft.key,
            annotations=draft.annotations,
            judgments=draft.judgments,
        )
        personal = MistakeCard.objects.filter(assignment=task, student_id=sub.student_id)
        active = []
        for item in grade.judgments:
            if item["verdict"] != "wrong":
                continue
            card, _ = MistakeCard.objects.update_or_create(
                assignment=task,
                student_id=sub.student_id,
                question_id=item["question_id"],
                defaults=dict(
                    grade=grade,
                    attachment_id=item["attachment_id"],
                    page=item["page"],
                    answer_id=item["answer_id"],
                    reason=item["reason"],
                    active=True,
                ),
            )
            active.append(card.pk)
        personal.exclude(pk__in=active).update(active=False)
    return batch


def personal_grades(user):
    return (
        ReleasedGrade.objects.filter(
            student=user, release__assignment__teaching_class__in=student_classes(user)
        )
        .select_related("submission", "release__assignment", "key")
        .order_by("-release_id")
    )


def solution_visible(grade):
    releases = AnswerRelease.objects.filter(assignment_id=grade.release.assignment_id)
    if grade.key_id:
        releases = releases.filter(revision_id=grade.key_id)
    return releases.exists()


def personal_cards(user):
    return MistakeCard.objects.filter(
        student=user,
        active=True,
        assignment__teaching_class__in=student_classes(user),
        grade__key__answerrelease__isnull=False,
    ).select_related("grade__release", "question__paper", "answer", "attachment", "assignment")
