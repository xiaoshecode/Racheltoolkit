"""Preview rows have no independent permission. Serve only through an authorized source."""
import hashlib
import io
import math
import uuid
from datetime import timedelta
from pathlib import Path

from django.db.models import Q
from django.utils import timezone
from PIL import Image, ImageOps
from pypdf import PdfReader

from .conversion import convert_office
from .models import PreviewAsset
from .services import ensure_written, reserve, stream

RENDERER = "pages-v1"
MAX_PAGES = 1000


def queue_preview(source, *, retry=False):
    if source.state != "ready":
        raise ValueError("Source is not ready.")
    asset, _ = PreviewAsset.objects.get_or_create(source_file=source, source_sha256=source.sha256,
                                                  renderer_version=RENDERER)
    if retry and asset.state == "failed":
        PreviewAsset.objects.filter(pk=asset.pk, state="failed").update(state="pending")
        asset.refresh_from_db()
    return asset


def pdf_manifest(data):
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted or not 0 < len(reader.pages) <= MAX_PAGES:
        raise ValueError("PDF is encrypted or outside the page limit.")
    manifest = []
    for index, page in enumerate(reader.pages, 1):
        box = page.cropbox
        width, height = float(box.width * page.user_unit), float(box.height * page.user_unit)
        rotation = page.rotation % 360
        if not all(math.isfinite(n) and n > 0 for n in (width, height)) or rotation % 90:
            raise ValueError("Invalid PDF page geometry.")
        manifest.append({"page": index, "width": width, "height": height, "rotation": rotation})
    return manifest


def render_preview(asset):
    """An expired worker may finish later; only its live claim can publish a manifest."""
    now = timezone.now()
    claim = uuid.uuid4()
    available = Q(state="pending") | Q(state="processing", started_at__lt=now - timedelta(minutes=5))
    if not PreviewAsset.objects.filter(available, pk=asset.pk).update(
            state="processing", claim_id=claim, started_at=now):
        asset.refresh_from_db()
        return asset
    owned = PreviewAsset.objects.filter(pk=asset.pk, state="processing", claim_id=claim)
    try:
        source = asset.source_file
        with stream(source) as original:
            data = original.read()
        output = source
        suffix = Path(source.original_name).suffix.lower()
        if data.startswith(b"%PDF-"):
            pages = pdf_manifest(data)
        elif data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
            with Image.open(io.BytesIO(data)) as picture:
                orientation = picture.getexif().get(274, 1)
                oriented = ImageOps.exif_transpose(picture)
                pages = [{"page": 1, "width": oriented.width, "height": oriented.height,
                          "rotation": 0, "exif_orientation": orientation}]
                oriented.close()
        elif suffix in (".doc", ".docx", ".ppt", ".pptx"):
            pdf = convert_office(data, suffix)
            pages = pdf_manifest(pdf)
            output = reserve("preview", source.id.hex + ".pdf", len(pdf), hashlib.sha256(pdf).hexdigest())
            ensure_written(output, pdf)
        else:
            raise ValueError("No renderer for this format.")
        owned.update(state="ready", page_manifest=pages, output_file=output, claim_id=None)
    except Exception:
        # Parser/Office errors can contain original text, file paths and URLs.
        # Preserve the source, expose only the retryable state.
        owned.update(state="failed", claim_id=None)
    asset.refresh_from_db()
    return asset


def prepare_preview(source, *, retry=False):
    """Synchronous worker entry; web requests queue Word conversions with queue_preview."""
    return render_preview(queue_preview(source, retry=retry))
