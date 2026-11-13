import http.client
from io import BytesIO
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, override_settings
from PIL import Image
from pypdf import PdfWriter

from .chat_uploads import MAX_UPLOAD, UploadError, upload_attachments, upload_to_provider, validate_attachment


class Session(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save(self):
        self.saved += 1


def text_file(name="notes.txt", data=b"Synthetic teaching material"):
    return SimpleUploadedFile(name, data, content_type="text/plain")


def image_file():
    data = BytesIO()
    Image.new("RGB", (16, 16), "white").save(data, "PNG")
    return SimpleUploadedFile("figure.png", data.getvalue(), content_type="image/png")


def pdf_file(encrypted=False):
    data = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    if encrypted:
        writer.encrypt("synthetic-password")
    writer.write(data)
    return SimpleUploadedFile("notes.pdf", data.getvalue(), content_type="application/pdf")


def office_file(name="notes.docx", malicious=False):
    parts = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml", ".xlsx": "xl/workbook.xml"}
    data = BytesIO()
    with ZipFile(data, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr(parts[name[name.rfind('.'):]], "<document/>")
        if malicious:
            package.writestr("../outside.txt", "outside")
    return SimpleUploadedFile(name, data.getvalue())


@override_settings(DEEPSEEK_API_KEY="synthetic-upload-key", DEEPSEEK_MODEL="auto")
class AttachmentEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.access = patch("school.chat_uploads.assistant_access", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.lease = patch("school.chat.Lease").start()
        self.lease.return_value.deadline = time.monotonic() + 300
        self.provider = patch("school.chat_uploads.upload_to_provider").start()
        self.provider.side_effect = lambda attachment, deadline: ("file-api-synthetic", int(time.time()) + 3600)

    def request(self, files=None, consent="true", authenticated=True, session=None):
        request = self.factory.post("/api/assistant/files/", {"files": files if files is not None else [text_file()], "consent": consent})
        request.user = SimpleNamespace(is_authenticated=authenticated, pk=17)
        request.session = session if session is not None else Session()
        request._dont_enforce_csrf_checks = True
        return request

    def result(self, request):
        response = upload_attachments(request)
        return response, json.loads(response.content)

    def test_authorization_configuration_consent_csrf_and_post_enforced(self):
        response, _ = self.result(self.request(authenticated=False))
        self.assertEqual(response.status_code, 401)
        self.access.return_value = False
        response, _ = self.result(self.request())
        self.assertEqual(response.status_code, 403)
        self.access.return_value = True
        with override_settings(DEEPSEEK_API_KEY=""):
            response, _ = self.result(self.request())
            self.assertEqual(response.status_code, 503)
        response, _ = self.result(self.request(consent="false"))
        self.assertEqual(response.status_code, 400)
        request = self.request()
        request._dont_enforce_csrf_checks = False
        self.assertEqual(upload_attachments(request).status_code, 403)
        get = self.factory.get("/api/assistant/files/")
        get.user = SimpleNamespace(is_authenticated=True, pk=17)
        get.session = Session()
        self.assertEqual(upload_attachments(get).status_code, 405)
        self.provider.assert_not_called()
        self.lease.assert_not_called()

    def test_validate_entire_selection_before_network(self):
        for files, code in (([], 400), ([text_file() for _ in range(4)], 400),
                ([text_file(), text_file("bad.exe")], 422),
                ([text_file("bad.png", b"not a picture")], 422),
                ([pdf_file(encrypted=True)], 422),
                ([office_file(malicious=True)], 422),
                ([text_file("binary.txt", b"\x00binary")], 422),
                ([text_file("bad.json", b"{broken")], 422)):
            with self.subTest(code=code, files=[file.name for file in files]):
                response, body = self.result(self.request(files))
                self.assertEqual(response.status_code, code)
                self.assertIn("error", body)
        self.provider.assert_not_called()
        self.lease.assert_not_called()

    def test_invalid_filename_is_a_safe_validation_error(self):
        with self.assertRaises(UploadError) as caught:
            validate_attachment(text_file("bad\ud800.txt"))
        self.assertEqual(caught.exception.status, 422)
        self.provider.assert_not_called()

    def test_total_size_and_text_limit_rejected(self):
        response, _ = self.result(self.request([text_file("large.txt", b"x" * (1024 * 1024 + 1))]))
        self.assertEqual(response.status_code, 413)
        oversized = text_file("large.pdf", b"x" * (MAX_UPLOAD + 1))
        response, _ = self.result(self.request([oversized]))
        self.assertEqual(response.status_code, 413)
        self.provider.assert_not_called()

    def test_success_uses_opaque_ids_and_persists_each_mapping(self):
        request = self.request([image_file(), pdf_file(), office_file("table.xlsx")])
        response, body = self.result(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(request.session.saved, 3)
        self.assertEqual(len(body["attachments"]), 3)
        self.assertNotIn("file-api", response.content.decode())
        self.assertNotIn("synthetic-upload-key", response.content.decode())
        self.lease.assert_called_once_with(17, duration=300)
        self.lease.return_value.close.assert_called_once()
        for entry in body["attachments"]:
            self.assertRegex(entry["id"], r"^[0-9a-f]{32}$")
            stored = request.session["assistant_attachments"][entry["id"]]
            self.assertEqual(stored["provider_id"], "file-api-synthetic")
            self.assertNotIn("data", stored)
            self.assertEqual(entry["name"], stored["name"])
        purposes = [call.args[0]["purpose"] for call in self.provider.call_args_list]
        self.assertEqual(purposes, ["user_data", "rich_layout_recognition", "user_data"])

    def test_partial_success_survives_later_provider_failure(self):
        self.provider.side_effect = [("file-api-first", int(time.time()) + 3600), UploadError("服务繁忙", 503)]
        request = self.request([text_file("one.txt"), text_file("two.txt")])
        response, body = self.result(request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(body["attachments"]), 1)
        self.assertEqual(request.session.saved, 1)
        self.assertEqual(len(request.session["assistant_attachments"]), 1)
        self.assertNotIn("file-api-first", response.content.decode())
        self.lease.return_value.close.assert_called_once()

    def test_expired_entries_pruned_and_session_limit_checked_before_upload(self):
        session = Session(assistant_attachments={str(i): {"expires_at": int(time.time()) + 500} for i in range(20)})
        response, _ = self.result(self.request(session=session))
        self.assertEqual(response.status_code, 429)
        self.provider.assert_not_called()
        session["assistant_attachments"]["0"]["expires_at"] = 0
        response, _ = self.result(self.request(session=session))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session["assistant_attachments"]), 20)
        self.assertNotIn("0", session["assistant_attachments"])


@override_settings(DEEPSEEK_API_KEY="synthetic-upload-key")
class ProviderUploadTests(SimpleTestCase):
    def provider(self, payload=None, status=200, content_type="application/json"):
        response = BytesIO(json.dumps(payload if payload is not None else {
            "id": "file-api-synthetic", "expires_at": int(time.time()) + 3600}).encode())
        response.status = status
        response.getheader = lambda key, default="": content_type
        connection = MagicMock()
        connection.getresponse.return_value = response
        return connection, response

    def test_fixed_host_multipart_order_expiry_and_cleanup(self):
        attachment = validate_attachment(pdf_file())
        connection, response = self.provider()
        with patch("school.chat_uploads.http.client.HTTPSConnection", return_value=connection) as constructor, \
                patch("school.chat_uploads.threading.Timer") as timer:
            identifier, _ = upload_to_provider(attachment, time.monotonic() + 300)
        self.assertEqual(identifier, "file-api-synthetic")
        self.assertEqual(constructor.call_args.args, ("api.deepseek.com",))
        self.assertLessEqual(constructor.call_args.kwargs["timeout"], 60)
        connection.request.assert_called_once()
        args = connection.request.call_args
        self.assertEqual(args.args, ("POST", "/files"))
        body = args.kwargs["body"]
        self.assertLess(body.index(b'name="purpose"'), body.index(b'name="file"'))
        self.assertIn(b"rich_layout_recognition", body)
        self.assertIn(b'name="expires_after[anchor]"', body)
        self.assertIn(b"created_at", body)
        self.assertIn(b"3600", body)
        self.assertEqual(args.kwargs["headers"]["Authorization"], "Bearer synthetic-upload-key")
        self.assertTrue(response.closed)
        connection.close.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        self.assertLessEqual(connection.sock.settimeout.call_args.args[0], 240)

    def test_upstream_errors_never_expose_raw_payload_or_key(self):
        for status, expected in ((400, 422), (402, 503), (429, 429), (500, 503), (302, 503), (504, 504)):
            connection, response = self.provider({"error": "synthetic-upload-key sensitive diagnostic"}, status)
            with self.subTest(status=status), patch("school.chat_uploads.http.client.HTTPSConnection", return_value=connection), \
                    patch("school.chat_uploads.threading.Timer"), self.assertRaises(UploadError) as caught:
                upload_to_provider(validate_attachment(text_file()), time.monotonic() + 300)
            self.assertEqual(caught.exception.status, expected)
            self.assertNotIn("synthetic", str(caught.exception))
            self.assertTrue(response.closed)

    def test_invalid_ids_expiry_and_non_json_are_rejected(self):
        for payload in ({"id": "arbitrary", "expires_at": int(time.time()) + 3600},
                {"id": "file-api-synthetic", "expires_at": 0},
                {"id": "file-api-synthetic", "expires_at": int(time.time()) + 99999},
                {"id": "file-api-synthetic"}, ["not an object"]):
            connection, response = self.provider(payload)
            with self.subTest(payload=payload), patch("school.chat_uploads.http.client.HTTPSConnection", return_value=connection), \
                    patch("school.chat_uploads.threading.Timer"), self.assertRaises(UploadError):
                upload_to_provider(validate_attachment(text_file()), time.monotonic() + 300)
            self.assertTrue(response.closed)

    def test_connect_failure_and_exhausted_deadline_are_sanitized(self):
        connection, _ = self.provider()
        connection.connect.side_effect = OSError("synthetic-upload-key")
        with patch("school.chat_uploads.http.client.HTTPSConnection", return_value=connection), self.assertRaises(UploadError) as caught:
            upload_to_provider(validate_attachment(text_file()), time.monotonic() + 300)
        self.assertNotIn("synthetic", str(caught.exception))
        connection.close.assert_called_once()
        with patch("school.chat_uploads.http.client.HTTPSConnection") as constructor, self.assertRaises(UploadError):
            upload_to_provider(validate_attachment(text_file()), time.monotonic() - 1)
        constructor.assert_not_called()
