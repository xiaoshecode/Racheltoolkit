from django.conf import settings
from django.db import models


class PaperVersion(models.Model):
    course = models.ForeignKey("school.Course", on_delete=models.PROTECT)
    code = models.CharField(max_length=80)
    version = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["course", "code", "version"], name="one_paper_version")
        ]


class Question(models.Model):
    paper = models.ForeignKey(PaperVersion, on_delete=models.PROTECT, related_name="questions")
    number = models.CharField(max_length=30)
    prompt = models.TextField()
    topic = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["paper", "number"], name="one_paper_question")]


class AnswerRevision(models.Model):
    question = models.ForeignKey(Question, on_delete=models.PROTECT)
    text = models.TextField()


class AnswerKeyRevision(models.Model):
    assignment = models.ForeignKey(
        "submissions.Assignment", on_delete=models.PROTECT, related_name="answer_revisions"
    )
    seq = models.PositiveIntegerField()
    paper = models.ForeignKey(PaperVersion, on_delete=models.PROTECT)
    file = models.ForeignKey("storage.StoredFile", on_delete=models.PROTECT, null=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["assignment", "seq"], name="one_key_revision")]


class KeyEntry(models.Model):
    key = models.ForeignKey(AnswerKeyRevision, on_delete=models.PROTECT, related_name="entries")
    answer = models.ForeignKey(AnswerRevision, on_delete=models.PROTECT)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["key", "position"], name="ordered_key_entries"),
            models.UniqueConstraint(fields=["key", "answer"], name="distinct_key_answers"),
        ]


class AnswerRelease(models.Model):
    assignment = models.ForeignKey(
        "submissions.Assignment", on_delete=models.PROTECT, related_name="answer_releases"
    )
    revision = models.OneToOneField(AnswerKeyRevision, on_delete=models.PROTECT)
    publisher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    due_at = models.DateTimeField()
    released_at = models.DateTimeField()
    request_key = models.CharField(max_length=100)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["assignment", "request_key"], name="answer_publish_retry")
        ]


class GradeDraft(models.Model):
    submission = models.OneToOneField(
        "submissions.Submission", on_delete=models.PROTECT, related_name="grade_draft"
    )
    grader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    edit_version = models.PositiveIntegerField()
    score = models.DecimalField(max_digits=10, decimal_places=2)
    feedback = models.TextField(blank=True)
    key = models.ForeignKey(AnswerKeyRevision, on_delete=models.PROTECT, null=True)
    annotations = models.JSONField(default=list)
    judgments = models.JSONField(default=list)


class GradeRelease(models.Model):
    assignment = models.ForeignKey(
        "submissions.Assignment", on_delete=models.PROTECT, related_name="grade_releases"
    )
    publisher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    at = models.DateTimeField(auto_now_add=True)
    request_key = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["assignment", "request_key"], name="grade_publish_retry")
        ]


class ScoreChange(models.Model):
    draft = models.ForeignKey(GradeDraft, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    previous = models.DecimalField(max_digits=10, decimal_places=2)
    updated = models.DecimalField(max_digits=10, decimal_places=2)
    at = models.DateTimeField(auto_now_add=True)


class ReleasedGrade(models.Model):
    release = models.ForeignKey(GradeRelease, on_delete=models.PROTECT, related_name="grades")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="grades")
    submission = models.ForeignKey("submissions.Submission", on_delete=models.PROTECT, related_name="grades")
    draft_version = models.PositiveIntegerField()
    score = models.DecimalField(max_digits=10, decimal_places=2)
    max_points = models.PositiveIntegerField()
    feedback = models.TextField(blank=True)
    key = models.ForeignKey(AnswerKeyRevision, on_delete=models.PROTECT, null=True)
    annotations = models.JSONField(default=list)
    judgments = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["release", "student"], name="grade_for_student_batch"),
            models.CheckConstraint(
                condition=models.Q(score__gte=0, score__lte=models.F("max_points")),
                name="released_score_valid",
            ),
        ]


class MistakeCard(models.Model):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    assignment = models.ForeignKey("submissions.Assignment", on_delete=models.PROTECT)
    question = models.ForeignKey(Question, on_delete=models.PROTECT)
    grade = models.ForeignKey(ReleasedGrade, on_delete=models.PROTECT, related_name="cards")
    attachment = models.ForeignKey("submissions.SubmissionAttachment", on_delete=models.PROTECT)
    page = models.PositiveIntegerField()
    answer = models.ForeignKey(AnswerRevision, on_delete=models.PROTECT)
    reason = models.CharField(max_length=400)
    active = models.BooleanField(default=True)
    reviewed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "student", "question"], name="one_personal_question"
            )
        ]
