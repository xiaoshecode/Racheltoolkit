import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from .backends import (ContentConflict, FaultBackend, LocalBackend, SeafileBackend,
                       StorageUnavailable, WriteUncertain)
from .models import StoredFile
from .services import (confirm_existing, ensure_written, file_response, release_absent_claim,
                       reserve, stream, verify)


class StorageTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=settings.RUNTIME)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = LocalBackend(self.root)
        patcher = patch("storage.services.get_backend", return_value=self.store)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.data = b"%PDF-1.7\nprivate test bytes"
        self.record = self.new()

    def new(self):
        return reserve("submission", "../real-name.pdf", len(self.data), hashlib.sha256(self.data).hexdigest())

    def test_server_path_bytes_and_response(self):
        record = self.record
        self.assertEqual(record.original_name, "real-name.pdf")
        self.assertEqual(record.path, f"/学生作业/{record.pk.hex}.pdf")
        self.assertFalse(verify(record))
        self.assertNotEqual(self.new().path, record.path)
        ensure_written(record, self.data)
        ensure_written(record, self.data)
        with stream(record) as result:
            self.assertEqual(result.read(), self.data)
        response = file_response(record, inline=True)
        self.assertEqual(b"".join(response.streaming_content), self.data)
        response.file_to_stream.close()
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(len(list(self.root.rglob("*.pdf"))), 1)

    def test_before_write_can_retry(self):
        self.factory.return_value = FaultBackend(self.root, "before_write")
        with self.assertRaises(StorageUnavailable):
            ensure_written(self.record, self.data)
        self.assertFalse(verify(self.record))
        self.record.refresh_from_db()
        self.assertIsNone(self.record.upload_claim)
        ensure_written(self.record, self.data)
        self.assertEqual(self.record.state, "ready")

    def test_after_write_restart_reconciles(self):
        self.factory.return_value = FaultBackend(self.root, "after_write")
        with self.assertRaises(WriteUncertain):
            ensure_written(self.record, self.data)
        self.record.refresh_from_db()
        self.assertIsNotNone(self.record.upload_claim)
        self.assertEqual(self.record.state, "pending")
        self.factory.return_value = LocalBackend(self.root)
        with patch.object(self.factory.return_value, "put", side_effect=AssertionError("rewrote object")):
            self.assertEqual(confirm_existing(self.record).state, "ready")
            ensure_written(self.record, self.data)
        self.assertEqual(len(list(self.root.rglob("*.pdf"))), 1)

    def test_unknown_absent_not_reuploaded_while_claim_exists(self):
        with patch.object(self.store, "put", side_effect=WriteUncertain("unknown")):
            with self.assertRaises(WriteUncertain):
                ensure_written(self.record, self.data)
        self.record.refresh_from_db()
        with patch.object(self.store, "put", side_effect=AssertionError("rewrote without reconciliation")):
            with self.assertRaises(WriteUncertain):
                ensure_written(self.record, self.data)
        self.assertTrue(release_absent_claim(self.record, self.record.upload_claim))
        ensure_written(self.record, self.data)

    def test_wrong_bytes_quarantined_never_overwritten(self):
        self.store.put(self.record.path, b"wrong bytes")
        with self.assertRaises(ContentConflict):
            ensure_written(self.record, self.data)
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, "quarantined")
        with self.store.open(self.record.path) as result:
            self.assertEqual(result.read(), b"wrong bytes")
        self.assertFalse(StoredFile.objects.filter(state="ready").exists())

    def test_metadata_mismatch_and_unsafe_input(self):
        with self.assertRaises(ContentConflict):
            ensure_written(self.record, b"")
        self.assertFalse(verify(self.record))
        with self.assertRaises(StorageUnavailable):
            self.store.open("../../etc/password")
        with self.assertRaises(ValueError):
            reserve("unknown", "a", 1, "bad")


class CloudContractTests(SimpleTestCase):
    def setUp(self):
        self.store = SeafileBackend("https://example.org", "fake-test-token")
        self.path = "/学生作业/" + "e" * 32 + ".pdf"

    def test_token_only_sent_to_api(self):
        filename = self.path.split("/")[-1]
        link = "https://example.org/seafhttp/download/signed"
        responses = [json.dumps({"dirent_list": [{"name": filename}]}).encode(),
                     json.dumps(link).encode(), b"hello"]
        with patch.object(self.store.opener, "open", side_effect=[io.BytesIO(r) for r in responses]) as opened:
            with self.store.open(self.path) as response:
                self.assertEqual(response.read(), b"hello")
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer fake-test-token")
        self.assertIsNone(requests[-1].get_header("Authorization"))

    def test_empty_dir_and_404_distinct(self):
        with patch.object(self.store.opener, "open", return_value=io.BytesIO(b'{"dirent_list":[]}')):
            self.assertIsNone(self.store.open(self.path))
        exc = HTTPError("https://fake/sensitive", 404, "sensitive", {}, io.BytesIO())
        with patch.object(self.store.opener, "open", side_effect=exc):
            with self.assertRaisesRegex(StorageUnavailable, "^Cloud transfer did not complete.$"):
                self.store.open(self.path)

    def test_rejects_untrusted_service_and_redirects(self):
        for url in ("http://example.org/leak", "https://foreign.org/secret",
                    "https://example.org@foreign.org/download"):
            with self.subTest(url=url), patch.object(self.store, "api", return_value=url):
                with self.assertRaises(StorageUnavailable):
                    self.store.put(self.path, b"test")
        from .backends import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, None, None, "https://foreign.org"))

    def test_upload_renamed_or_disconnected_is_uncertain(self):
        with patch.object(self.store, "link", return_value="https://example.org/upload-token"):
            for attempt in (io.BytesIO(b'[{"name":"auto-renamed (1).pdf"}]'), TimeoutError()):
                with self.subTest(attempt=type(attempt)), patch.object(
                        self.store.opener, "open", side_effect=[attempt]) as opened:
                    with self.assertRaises(WriteUncertain):
                        self.store.put(self.path, b"test")
                    req = opened.call_args.args[0]
                    self.assertNotIn("Authorization", req.headers)
                    self.assertIn(b'name="replace"\r\n\r\n0', req.data)
