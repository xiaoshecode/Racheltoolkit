"""Run with locked app dependencies and the built converter on a Linux worker."""
import hashlib
import io
import os
import sys
from pathlib import Path
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from pypdf import PdfReader
from storage.conversion import run_renderer


def synthetic_docx():
    buf = io.BytesIO()
    parts = {
        "[Content_Types].xml": '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
        <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
        <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>''',
        "_rels/.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
        <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''',
        "word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:body><w:p><w:r><w:t>Classroom fixture page one</w:t><w:br w:type="page"/>
        <w:t>Classroom fixture page two</w:t></w:r></w:p></w:body></w:document>''',
    }
    with ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


def verify_submissions():
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.core.management import call_command, CommandError
    from django.test import Client
    from django.utils import timezone
    from school.models import Term, TeachingClass, Course, Enrollment
    from storage.models import PreviewAsset
    from submissions.models import Assignment

    call_command("migrate", verbosity=0)
    term = Term.objects.create(code="smoke")
    course = Course.objects.create(code="P", title="Fixture")
    group = TeachingClass.objects.create(term=term, course=course, code="X")
    student = get_user_model().objects.create_user(username="00001")
    Enrollment.objects.create(teaching_class=group, student=student)
    now = timezone.now()
    task = Assignment.objects.create(teaching_class=group, title="Word upload", published_at=now,
                                     due_at=now + timedelta(hours=1))
    client = Client(HTTP_HOST="localhost")
    client.force_login(student)
    modern = synthetic_docx()
    legacy = run_renderer(modern, "fixture-doc")
    for version, (name, data) in enumerate([("page.docx", modern), ("page.doc", legacy)], 1):
        response = client.post(f"/assignments/{task.pk}/submissions/", {
            "key": f"word-{version}", "files": SimpleUploadedFile(name, data),
        })
        assert response.status_code == 201, response.content
        receipt = response.json()
        assert receipt["version"] == version and len(receipt["attachments"]) == 1
        part = receipt["attachments"][0]
        assert part["sha256"] == hashlib.sha256(data).hexdigest()
        assert client.get(receipt["url"]).status_code == 200
        base = part["url"].removesuffix("download/")
        assert client.get(base + "preview-status/").json()["state"] == "pending"
        asset = PreviewAsset.objects.get(source_file__sha256=part["sha256"])
        # Real container startup failure, followed by recovery on the same source.
        with patch.dict(os.environ, TEACH_OFFICE_IMAGE="teaching-smoke-missing-image:never-pull"):
            try:
                call_command("render_previews", asset=asset.pk)
            except CommandError:
                pass
            else:
                raise AssertionError("Missing worker image should fail closed")
        assert client.get(base + "preview-status/").json()["state"] == "failed"
        original = client.get(part["url"])
        assert b"".join(original.streaming_content) == data
        call_command("render_previews", asset=asset.pk, retry_failed=True)
        ready = client.get(base + "preview-status/").json()
        assert ready["state"] == "ready" and len(ready["pages"]) == 2
        result = client.get(base + "preview/")
        assert result.status_code == 200
        pdf = b"".join(result.streaming_content)
        reader = PdfReader(io.BytesIO(pdf))
        assert len(reader.pages) == 2 and "page two" in reader.pages[1].extract_text()
        print(f"PASS {name}: response=201 version={version} source_bytes={len(data)} "
              f"sha256={part['sha256']} worker=failed,retried,ready pages=2 preview_bytes={len(pdf)}")


if __name__ == "__main__":
    runtime = Path(__file__).resolve().parents[3] / ".local"
    runtime.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=runtime) as run:
        os.environ.update(TEACH_STORAGE_BACKEND="local", TEACH_DATA_DIR=run, TEACH_DB=str(Path(run) / "test.sqlite3"))
        verify_submissions()
