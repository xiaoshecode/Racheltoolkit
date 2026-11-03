import uuid

from django.conf import settings
from django.db import models

class Assignment(models.Model):
    teaching_class = models.ForeignKey("school.TeachingClass", on_delete=models.PROTECT, related_name="assignments")
    chapter_key = models.UUIDField(null=True, blank=True)
    title = models.CharField(max_length=180)
    instructions = models.TextField(blank=True)
    published_at = models.DateTimeField()
    due_at = models.DateTimeField()
    max_points = models.PositiveIntegerField(default=100)
    class Meta:
        indexes = [models.Index(fields=["teaching_class", "due_at"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(due_at__gt=models.F("published_at")), name="assignment_due_after_publish"),
            models.CheckConstraint(condition=models.Q(max_points__gt=0), name="positive_max_points"),
        ]
    def __str__(self):
        return self.title

class DeadlineChange(models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT)
    old_due = models.DateTimeField()
    new_due = models.DateTimeField()
    reason = models.CharField(max_length=300)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    at = models.DateTimeField(auto_now_add=True)

class Submission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT, related_name="submissions")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submissions")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submitted_versions", null=True)
    source = models.CharField(max_length=12)
    note = models.CharField(max_length=300, blank=True)
    version = models.PositiveIntegerField()
    retry_key = models.CharField(max_length=100)
    state = models.CharField(max_length=16, default="pending")
    admitted_at = models.DateTimeField()
    received_at = models.DateTimeField(null=True)
    claim = models.UUIDField(null=True)
    claimed_at = models.DateTimeField(null=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["assignment","student","version"], name="one_student_version"),
            models.UniqueConstraint(fields=["assignment","student","retry_key"], name="one_request_key"),
            models.CheckConstraint(condition=models.Q(version__gt=0), name="positive_submission_version"),
            models.CheckConstraint(condition=(models.Q(state="confirmed", received_at__isnull=False) |
                                  (~models.Q(state="confirmed") & models.Q(received_at__isnull=True))),
                                  name="receipt_only_for_confirmed"),
        ]
        indexes = [models.Index(fields=["assignment","student","state","version"])]

class SubmissionAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="attachments")
    position = models.PositiveSmallIntegerField()
    file = models.OneToOneField("storage.StoredFile", on_delete=models.PROTECT)
    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["submission","position"], name="one_attachment_per_position"),
            models.CheckConstraint(condition=models.Q(position__gte=1, position__lte=10), name="valid_page_position"),
        ]

