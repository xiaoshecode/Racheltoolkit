"""No public file route. Callers must authorize through the owning course/assignment before use."""
import hashlib
import http.client
import re
import uuid
from pathlib import Path
from tempfile import SpooledTemporaryFile

from django.conf import settings
from django.http import FileResponse
from django.utils import timezone

from .backends import (CHUNK, DIRECTORIES, ContentConflict, StorageUnavailable,
                       WriteUncertain, get_backend, library_id)
from .models import StoredFile


def reserve(kind, original_name, size, sha256):
    identifier = uuid.uuid4()
    name = str(original_name).replace("\\", "/").split("/")[-1]
    if (kind not in DIRECTORIES or not name or len(name) > 255 or size < 0
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or not re.fullmatch("[0-9a-f]{64}", sha256)):
        raise ValueError("Invalid file metadata.")
    suffix = Path(name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ""
    return StoredFile.objects.create(id=identifier, kind=kind, library_id=library_id(),
                                     path=f"/{DIRECTORIES[kind]}/{identifier.hex}{suffix}",
                                     original_name=name, size=size, sha256=sha256)


def _check(record, target=None):
    if record.library_id != library_id():
        raise StorageUnavailable("Object belongs to another library.")
    if record.state == "quarantined":
        raise ContentConflict("Object is quarantined.")
    source = get_backend().open(record.path)
    if source is None:
        return False
    digest = hashlib.sha256()
    total = 0
    try:
        with source:
            while block := source.read(min(CHUNK, record.size - total + 1)):
                total += len(block)
                if total > record.size:
                    break
                digest.update(block)
                if target is not None:
                    target.write(block)
    except (OSError, http.client.HTTPException):
        raise StorageUnavailable("Could not read file bytes.") from None
    if total != record.size or digest.hexdigest() != record.sha256:
        StoredFile.objects.filter(pk=record.pk).update(state="quarantined")
        record.state = "quarantined"
        raise ContentConflict("Remote bytes differ from the registered size or checksum.")
    return True


def verify(record):
    return _check(record)


def _ready(record):
    if not StoredFile.objects.filter(pk=record.pk).exclude(state="quarantined").update(
            state="ready", upload_claim=None, claimed_at=None):
        raise ContentConflict("Object is quarantined.")
    record.state = "ready"
    return record


def confirm_existing(record):
    """After a process interruption, promote only a fully verified existing object."""
    return _ready(record) if verify(record) else None


def ensure_written(record, content):
    if len(content) != record.size or hashlib.sha256(content).hexdigest() != record.sha256:
        raise ContentConflict("Upload differs from reserved bytes.")
    existing = confirm_existing(record)
    if existing is not None:
        return existing
    if record.state == "ready":
        raise ContentConflict("Previously confirmed file is missing.")
    claim = uuid.uuid4()
    acquired = StoredFile.objects.filter(pk=record.pk, state="pending", upload_claim=None).update(
        upload_claim=claim, claimed_at=timezone.now())
    if not acquired:
        # Another writer may have just completed. Never send a second network upload.
        existing = confirm_existing(record)
        if existing:
            return existing
        raise WriteUncertain("An earlier upload must be reconciled.")
    try:
        get_backend().put(record.path, content)
    except StorageUnavailable:
        StoredFile.objects.filter(pk=record.pk, upload_claim=claim).update(upload_claim=None, claimed_at=None)
        raise
    try:
        stored = confirm_existing(record)
    except StorageUnavailable:
        raise WriteUncertain("Could not verify the uploaded bytes.") from None
    if stored is None:
        raise WriteUncertain("Upload has not been confirmed at its reserved path.")
    return stored


def release_absent_claim(record, claim):
    """Operator has established that the displaced writer and remote request ended."""
    if confirm_existing(record):
        return False
    return bool(StoredFile.objects.filter(pk=record.pk, state="pending", upload_claim=claim).update(
        upload_claim=None, claimed_at=None))


def stream(record):
    if record.state != "ready":
        raise StorageUnavailable("File is not ready.")
    # Verify before returning any bytes, including when the remote server fails mid-read.
    buffer = SpooledTemporaryFile(max_size=2 * CHUNK, dir=settings.RUNTIME)
    try:
        if not _check(record, buffer):
            raise StorageUnavailable("Confirmed file is missing.")
        buffer.seek(0)
        return buffer
    except BaseException:
        buffer.close()
        raise


def file_response(record, inline=False):
    with_open = stream(record)
    header = with_open.read(12)
    with_open.seek(0)
    content_type = "application/octet-stream"
    if header.startswith(b"%PDF-"):
        content_type = "application/pdf"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    elif header.startswith(b"\xff\xd8\xff"):
        content_type = "image/jpeg"
    response = FileResponse(with_open, as_attachment=not inline or content_type == "application/octet-stream",
                            filename=record.original_name, content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
