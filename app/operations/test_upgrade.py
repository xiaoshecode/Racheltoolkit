import os
from pathlib import Path
import subprocess
import sys
import tempfile

from django.conf import settings
from django.test import SimpleTestCase


class UpgradeTests(SimpleTestCase):
    def test_legacy_file_becomes_first_page_with_unchanged_receipt_and_grade_reference(self):
        script = """
from datetime import timedelta
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from uuid import uuid4

executor=MigrationExecutor(connection)
target=[("submissions","0001_initial")]
executor.migrate(target)
old=executor.loader.project_state(target).apps
term=old.get_model("school","Term").objects.create(code="Archive")
course=old.get_model("school","Course").objects.create(code="M",title="数学")
group=old.get_model("school","TeachingClass").objects.create(term=term,course=course,code="X")
user=old.get_model("auth","User").objects.create(username="0009")
now=timezone.now()
work=old.get_model("submissions","Assignment").objects.create(teaching_class=group,title="历史作业",
    published_at=now-timedelta(days=2),due_at=now-timedelta(days=1))
file=old.get_model("storage","StoredFile").objects.create(kind="submission",library_id="local",
    path="/学生作业/"+uuid4().hex+".pdf",original_name="legacy.pdf",size=3,sha256="a"*64,state="ready")
sub=old.get_model("submissions","Submission").objects.create(student=user,assignment=work,file=file,
    version=4,retry_key="original",state="confirmed",admitted_at=now,received_at=now)
# Prior reporting software kept its snapshot by the historical submission ID.
with connection.cursor() as c:
    c.execute("CREATE TABLE archived_score (submission_id char(32) REFERENCES submissions_submission(id), score INTEGER)")
    c.execute("INSERT INTO archived_score VALUES (%s, %s)",[sub.pk.hex,0])
executor=MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
from submissions.models import Submission
saved=Submission.objects.get(pk=sub.pk)
assert saved.version==4 and saved.retry_key=="original" and saved.received_at==now
assert saved.source=="unknown" and saved.submitted_by_id is None
page=saved.attachments.get()
assert page.position==1 and page.file.pk==file.pk and page.file.path==file.path
with connection.cursor() as c:
    assert c.execute("SELECT * FROM archived_score").fetchone()==(sub.pk.hex,0)
    assert not c.execute("PRAGMA foreign_key_check").fetchall()
print("PASS V4 original receipt, sole attachment position 1, unknown actor, historical zero score retained")
"""
        with tempfile.TemporaryDirectory(dir=settings.RUNTIME) as tmp:
            env = dict(
                os.environ,
                TEACH_DB=str(Path(tmp) / "old.sqlite3"),
                TEACH_DATA_DIR=tmp,
                PYTHONIOENCODING="utf-8",
                DJANGO_SETTINGS_MODULE="config.settings",
            )
            result = subprocess.run(
                [sys.executable, str(settings.BASE_DIR / "app/manage.py"), "shell", "-c", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=40,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS V4", result.stdout)
