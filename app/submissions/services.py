import re
import uuid
from datetime import timedelta

from django.apps import apps
from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.utils import timezone

from school.permissions import student_classes, teacher_classes
from storage import services as objects
from storage.backends import ContentConflict, StorageUnavailable, WriteUncertain
from .models import Assignment, DeadlineChange, Submission, SubmissionAttachment
from .validation import Refused

def authorize(assignment, student, actor, behalf):
    if not student_classes(student).filter(pk=assignment.teaching_class_id).exists():
        raise Http404
    if behalf:
        if not teacher_classes(actor).filter(pk=assignment.teaching_class_id).exists():
            raise Http404
    elif student.pk != actor.pk:
        raise Http404
    if assignment.published_at > timezone.now():
        raise Http404

def attachments(submission):
    return submission.attachments.select_related("file").order_by("position")

def record_result(sub, claim, state):
    attrs = {"state": state, "claim": None, "claimed_at": None}
    if state == "confirmed":
        attrs["received_at"] = timezone.now()
    with transaction.atomic():
        if state == "confirmed" and (not sub.attachments.exists() or sub.attachments.exclude(file__state="ready").exists()):
            raise RuntimeError("Cannot issue a receipt for unverified bytes")
        Submission.objects.filter(pk=sub.pk, claim=claim, state="uploading").update(**attrs)
        sub.refresh_from_db()
    return sub

def submit(assignment_id, student, actor, behalf, note, key, items):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", key or ""):
        raise Refused("缺少有效的本次提交编号。")
    if len(note) > 300 or (behalf and not note.strip()):
        raise Refused("请填写代交说明（最多 300 字）。")
    source = "teacher" if behalf else "student"
    with transaction.atomic():
        assignment = Assignment.objects.get(pk=assignment_id)
        authorize(assignment, student, actor, behalf)
        sub = Submission.objects.filter(assignment=assignment, student=student, retry_key=key).first()
        if sub:
            manifest = [(a.file.original_name,a.file.size,a.file.sha256) for a in attachments(sub)]
            if (sub.submitted_by_id != actor.pk or sub.source != source or sub.note != note
                    or manifest != [(i.name,len(i.data),i.sha) for i in items]):
                raise Refused("该提交编号已用于另一份清单或操作人。", 409)
            if sub.state == "confirmed":
                return sub, 200
            if sub.state == "quarantined":
                raise Refused("文件核验冲突，已隔离。", 409)
            if sub.claim:
                return sub, 202
        else:
            admitted = timezone.now()
            if admitted >= assignment.due_at:
                raise Refused("作业已截止。", 409)
            highest = Submission.objects.filter(assignment=assignment, student=student).aggregate(n=Max("version"))["n"]
            sub = Submission.objects.create(assignment=assignment, student=student, submitted_by=actor,
                source=source, note=note, version=(highest or 0)+1, retry_key=key, admitted_at=admitted)
            for index, item in enumerate(items, 1):
                file = objects.reserve("submission", item.name, len(item.data), item.sha)
                SubmissionAttachment.objects.create(submission=sub, position=index, file=file)
        claim = uuid.uuid4()
        sub.claim, sub.claimed_at, sub.state = claim, timezone.now(), "uploading"
        sub.save(update_fields=["claim","claimed_at","state"])

    try:
        for item, attachment in zip(items, attachments(sub), strict=True):
            objects.ensure_written(attachment.file, item.data)
    except ContentConflict:
        record_result(sub, claim, "quarantined")
        raise Refused("文件内容校验冲突，已隔离。", 409)
    except WriteUncertain:
        return record_result(sub, claim, "needs_check"), 202
    except StorageUnavailable:
        return record_result(sub, claim, "retryable"), 202
    sub = record_result(sub, claim, "confirmed")
    return sub, 201 if sub.state == "confirmed" else 202

def reconcile(sub, repair=False, stopped=False):
    """For live claims only an operator who confirmed worker termination may take over."""
    observed_claim = sub.claim
    if observed_claim and (not stopped or not sub.claimed_at or sub.claimed_at > timezone.now()-timedelta(minutes=5)):
        return "active"
    if not repair:
        return sub.state
    with transaction.atomic():
        claim = uuid.uuid4()
        changed = Submission.objects.filter(pk=sub.pk, claim=observed_claim).exclude(
            state__in=["confirmed","quarantined"]).update(state="uploading", claim=claim, claimed_at=timezone.now())
        if not changed:
            return "unchanged"
    try:
        missing = False
        for attachment in attachments(sub):
            file = attachment.file
            if objects.confirm_existing(file):
                continue
            missing = True
            if stopped and file.upload_claim:
                objects.release_absent_claim(file, file.upload_claim)
        state = "retryable" if missing else "confirmed"
    except ContentConflict:
        state = "quarantined"
    except (WriteUncertain, StorageUnavailable):
        state = "needs_check"
    return record_result(sub, claim, state).state

@transaction.atomic
def extend(pk, actor, until, reason):
    assignment = Assignment.objects.get(pk=pk, teaching_class__in=teacher_classes(actor))
    if apps.is_installed("grading") and apps.get_model("grading","AnswerRelease").objects.filter(assignment=assignment).exists():
        raise Refused("本班已发布答案，不能延长截止。", 409)
    if until <= assignment.due_at or not reason.strip() or len(reason)>300:
        raise Refused("延期须晚于原截止时间并填写原因。")
    DeadlineChange.objects.create(assignment=assignment, actor=actor, old_due=assignment.due_at, new_due=until, reason=reason)
    assignment.due_at = until
    assignment.save(update_fields=["due_at"])

