import uuid
from pathlib import Path

from django.conf import settings
from django.db import models


class CourseRevision(models.Model):
    course = models.ForeignKey("school.Course", on_delete=models.PROTECT, related_name="revisions")
    seq = models.PositiveIntegerField()
    edit_version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=160)
    intro = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["course", "seq"], name="catalog_revision_sequence"),
            models.UniqueConstraint(fields=["course"], condition=models.Q(published_at__isnull=True),
                                    name="catalog_one_draft"),
        ]


class CoursePublication(models.Model):
    course = models.OneToOneField("school.Course", on_delete=models.PROTECT, related_name="publication")
    revision = models.OneToOneField(CourseRevision, on_delete=models.PROTECT)


class Section(models.Model):
    revision = models.ForeignKey(CourseRevision, on_delete=models.CASCADE, related_name="sections")
    stable_key = models.UUIDField(default=uuid.uuid4)
    position = models.PositiveIntegerField()
    title = models.CharField(max_length=160)
    body = models.TextField(blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["revision", "stable_key"], name="catalog_chapter_identity"),
            models.UniqueConstraint(fields=["revision", "position"], name="catalog_chapter_order"),
        ]


class Material(models.Model):
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="materials")
    stable_key = models.UUIDField(default=uuid.uuid4)
    position = models.PositiveIntegerField()
    title = models.CharField(max_length=160)
    original_file = models.ForeignKey("storage.StoredFile", on_delete=models.PROTECT, related_name="+")
    preview_file = models.ForeignKey("storage.StoredFile", on_delete=models.PROTECT, related_name="+", null=True)

    @property
    def format(self):
        return Path(self.original_file.original_name).suffix.lstrip(".").upper()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["section", "stable_key"], name="catalog_material_identity"),
            models.UniqueConstraint(fields=["section", "position"], name="catalog_material_order"),
        ]
