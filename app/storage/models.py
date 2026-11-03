import uuid

from django.db import models


class StoredFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=24)
    library_id = models.CharField(max_length=100)
    path = models.CharField(max_length=255)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="pending")
    upload_claim = models.UUIDField(null=True)
    claimed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("library_id", "path"), name="file_immutable_location"),
            models.CheckConstraint(condition=models.Q(state__in=["pending", "ready", "quarantined"]),
                                   name="file_valid_state"),
        ]


class PreviewAsset(models.Model):
    source_file = models.ForeignKey(StoredFile, on_delete=models.PROTECT, related_name="previews")
    source_sha256 = models.CharField(max_length=64)
    renderer_version = models.CharField(max_length=100)
    state = models.CharField(max_length=16, default="pending")
    output_file = models.ForeignKey(StoredFile, on_delete=models.PROTECT, null=True,
                                    related_name="rendered_previews")
    page_manifest = models.JSONField(default=list)
    # A worker lease can be reclaimed after a process stops.
    started_at = models.DateTimeField(null=True)
    claim_id = models.UUIDField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("source_file", "source_sha256", "renderer_version"),
                                    name="preview_version_unique"),
        ]
