import hashlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from PIL import Image
from pypdf import PdfWriter

from .models import PreviewAsset
from .preview_views import preview_file, preview_status
from .previews import prepare_preview, queue_preview
from .services import ensure_written, reserve, stream


def two_pages():
    out = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=100, height=200).rotate(90)
    writer.write(out)
    return out.getvalue()


class PreviewTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory(dir=settings.RUNTIME)
        self.addCleanup(tmp.cleanup)
        config = override_settings(TEACH_STORAGE_BACKEND="local", LOCAL_STORAGE_ROOT=Path(tmp.name))
        config.enable()
        self.addCleanup(config.disable)
        self.factory = RequestFactory()

    def file(self, name, data):
        source = reserve("submission", name, len(data), hashlib.sha256(data).hexdigest())
        return ensure_written(source, data)

    def test_pdf_pages_rotation_source_binding_and_reuse(self):
        source = self.file("v1.pdf", two_pages())
        asset = prepare_preview(source)
        self.assertEqual(asset.state, "ready")
        self.assertEqual(asset.page_manifest, [
            {"page": 1, "width": 612.0, "height": 792.0, "rotation": 0},
            {"page": 2, "width": 100.0, "height": 200.0, "rotation": 90}])
        self.assertEqual(asset.source_sha256, source.sha256)
        self.assertEqual(prepare_preview(source).pk, asset.pk)
        self.assertEqual(PreviewAsset.objects.count(), 1)
        self.assertEqual(asset.output_file_id, source.pk)
        response = preview_file(self.factory.get("/preview"), source)
        self.assertEqual(b"".join(response.streaming_content), two_pages())
        response.file_to_stream.close()

    def test_photo_and_unknown_asset(self):
        out = io.BytesIO()
        Image.new("RGB", (22, 17), "yellow").save(out, format="PNG")
        source = self.file("second.png", out.getvalue())
        self.assertEqual(preview_file(self.factory.get("/preview"), source).status_code, 404)
        asset = prepare_preview(source)
        self.assertEqual((asset.page_manifest[0]["width"], asset.page_manifest[0]["height"]), (22, 17))
        self.assertEqual(asset.output_file_id, source.pk)
        response = preview_status(self.factory.get("/status"), source)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])

    def test_office_failure_retry_new_output_and_file_integrity(self):
        source = self.file("attempt.docx", b"synthetic office fixture")
        self.assertEqual(queue_preview(source).state, "pending")
        with patch("storage.previews.convert_office", side_effect=RuntimeError("do not expose")):
            asset = prepare_preview(source)
        self.assertEqual(asset.state, "failed")
        self.assertIsNone(asset.output_file_id)
        self.assertEqual(preview_status(self.factory.post("/retry", {"action": "retry"}), source).status_code, 404)
        with stream(source) as original:
            self.assertEqual(original.read(), b"synthetic office fixture")
        with patch("storage.previews.convert_office", return_value=two_pages()):
            asset = prepare_preview(source, retry=True)
        self.assertEqual(asset.state, "ready")
        self.assertEqual(len(asset.page_manifest), 2)
        self.assertNotEqual(asset.output_file_id, source.pk)
        self.assertEqual(asset.source_sha256, source.sha256)
        with stream(asset.output_file) as derived:
            self.assertEqual(derived.read(), two_pages())
