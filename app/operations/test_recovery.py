import json
import os
import subprocess
import sys
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path
from time import monotonic

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from grading.services import publish_answers, publish_grades, save_answers, save_grade
from storage.previews import prepare_preview
from storage.models import StoredFile
from storage.office.smoke import synthetic_docx
from submissions.models import Assignment, Submission
from submissions.tests import WorkflowBase, pdf, photo


def probe(root, database, code, *, app=None):
    env = os.environ.copy()
    env.update(
        TEACH_STORAGE_BACKEND="local",
        TEACH_DATA_DIR=str(root),
        TEACH_DB=str(database),
        DJANGO_SETTINGS_MODULE="config.settings",
        PYTHONIOENCODING="utf-8",
    )
    program = Path(app or settings.BASE_DIR / "app") / "manage.py"
    result = subprocess.run(
        [sys.executable, str(program), "shell", "-c", code],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=45,
    )
    return json.loads(result.stdout.splitlines()[-1])


INSPECT = """
import json,hashlib
from submissions.models import Submission
from storage.services import stream
data={}
for sub in Submission.objects.filter(state='confirmed'):
    pages=[]
    for att in sub.attachments.select_related('file').order_by('position'):
        with stream(att.file) as reader:
            pages.append(hashlib.file_digest(reader,'sha256').hexdigest())
    data[str(sub.pk)]=pages
print(json.dumps(data))
"""


class RecoveryTests(WorkflowBase):
    def test_quarantined_failed_intake_does_not_block_backup_of_signed_receipts(self):
        self.assertEqual(self.send("good", ("good.png", photo("green"))).status_code, 201)
        self.assertEqual(self.send("damaged", ("bad.png", photo("red"))).status_code, 201)
        bad = Submission.objects.get(retry_key="damaged")
        Submission.objects.filter(pk=bad.pk).update(state="quarantined", received_at=None)
        bad_file = bad.attachments.get().file
        StoredFile.objects.filter(pk=bad_file.pk).update(state="quarantined")
        backup = self.folder / "snapshot"
        call_command("snapshot_teaching", backup)
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf8"))
        self.assertEqual(
            [r["copied"] for r in manifest["objects"] if r["id"] == bad_file.pk.hex], [False]
        )
        self.assertEqual(sum(row["copied"] for row in manifest["objects"]), 1)
        good = Submission.objects.get(retry_key="good").attachments.get().file
        StoredFile.objects.filter(pk=good.pk).update(state="quarantined")
        with self.assertRaisesRegex(CommandError, "outside a failed intake"):
            call_command("snapshot_teaching", self.folder / "invalid-backup")

    def test_snapshot_then_new_submission_restore_and_rollback(self):
        root = self.folder / "live"
        root.mkdir()
        with override_settings(RUNTIME=root, LOCAL_STORAGE_ROOT=root / "private/storage"):
            self.exercise(root)

    def exercise(self, root):
        result = self.send("before", ("work.docx", synthetic_docx()))
        self.assertEqual(result.status_code, 201)
        sub = Submission.objects.get()
        item = sub.attachments.get()
        with patch("storage.previews.convert_office", return_value=pdf()):
            preview = prepare_preview(item.file)
        self.assertEqual(preview.state, "ready")
        self.assertNotEqual(preview.source_file_id, preview.output_file_id)
        key = save_answers(
            self.teacher,
            self.work.pk,
            0,
            "P",
            1,
            [{"number": "2", "prompt": "计算", "answer": "12"}],
        )
        entry = key.entries.get()
        geom = preview.page_manifest[1]
        annotation = {
            "attachment_id": str(item.pk),
            "page": 2,
            "width": geom["width"],
            "height": geom["height"],
            "renderer_version": preview.renderer_version,
            "rotation": 0,
            "x": 0.2,
            "y": 0.6,
            "text": "复查计算",
            "source_sha256": item.file.sha256,
            "type": "text",
        }
        issue = {
            "question_id": entry.answer.question_id,
            "attachment_id": str(item.pk),
            "page": 2,
            "verdict": "wrong",
            "reason": "计算错误",
        }
        draft = save_grade(self.teacher, sub.pk, 0, "16", "再练一次", key.pk, [annotation], [issue])
        publish_grades(self.teacher, self.work.pk, "grade", {str(sub.pk): draft.edit_version})
        Assignment.objects.filter(pk=self.work.pk).update(
            due_at=timezone.now() - timedelta(seconds=1)
        )
        publish_answers(self.teacher, self.work.pk, key.pk, "answer")
        snapshot = self.folder / "snapshot"
        started = monotonic()
        call_command("snapshot_teaching", snapshot)
        snapshot_s = round(monotonic() - started, 3)
        # A distinct open assignment accepts new work after the earlier class has published answers.
        work2 = Assignment.objects.create(
            teaching_class=self.x,
            title="补练",
            published_at=timezone.now(),
            due_at=timezone.now() + timedelta(days=1),
        )
        added = self.send(
            "after",
            ("p2.png", photo("blue")),
            ("p1.png", photo("red")),
            url=f"/assignments/{work2.pk}/submissions/",
        )
        self.assertEqual(added.status_code, 201)
        live_db = Path(connection.settings_dict["NAME"])
        before = probe(root, live_db, INSPECT)
        restored = self.folder / "restored"
        started = monotonic()
        call_command("restore_teaching", snapshot, restored)
        restore_s = round(monotonic() - started, 3)
        self.assertEqual(
            probe(restored, restored / "school.sqlite3", INSPECT),
            {str(sub.pk): before[str(sub.pk)]},
        )
        check = """
from grading.models import MistakeCard,GradeRelease,AnswerRelease
from storage.models import PreviewAsset
from storage.services import stream
import json, hashlib
card=MistakeCard.objects.get()
asset=PreviewAsset.objects.get()
with stream(asset.output_file) as output:
    output_sha=hashlib.file_digest(output,'sha256').hexdigest()
print(json.dumps([GradeRelease.objects.count(),AnswerRelease.objects.count(),card.page,card.question.number,
card.question.paper.code,card.answer.text,card.grade.annotations[0]['x'],card.grade.annotations[0]['y'],
card.grade.annotations[0]['source_sha256'],asset.page_manifest[1]['page'],output_sha]))
"""
        self.assertEqual(
            probe(restored, restored / "school.sqlite3", check),
            [1, 1, 2, "2", "P", "12", 0.2, 0.6, item.file.sha256, 2, preview.output_file.sha256],
        )
        previous = os.getenv("TEACH_ROLLBACK_APP")
        if previous:
            self.assertEqual(probe(root, live_db, INSPECT, app=previous), before)
        self.assertEqual(probe(root, live_db, INSPECT), before)
        self.assertEqual(len(before), 2)
        self.assertEqual(len(before[added.json()["receipt"]]), 2)
        print(
            "M7b "
            + json.dumps(
                {
                    "snapshot_s": snapshot_s,
                    "restore_s": restore_s,
                    "snapshot_receipts": 1,
                    "restored_receipts": 1,
                    "live_receipts": 2,
                    "hashes": before,
                    "page": 2,
                    "grade_batches": 1,
                    "answer_releases": 1,
                    "card_answer": "P2",
                    "preview_sha": preview.output_file.sha256,
                    "rollback_build_exercised": bool(previous),
                    "missing": 0,
                },
                sort_keys=True,
            )
        )
        object_path = next(p for p in (snapshot / "objects").rglob("*") if p.is_file())
        object_path.write_bytes(b"corruption")
        bad = self.folder / "invalid"
        with self.assertRaises(CommandError):
            call_command("restore_teaching", snapshot, bad)
        self.assertFalse(bad.exists())

    def test_cloud_library_identity_is_preserved_in_isolated_local_restore(self):
        root = self.folder / "cloud-simulation"
        root.mkdir()
        (root / "restored-library.txt").write_text("simulation-cloud-library", encoding="utf8")
        with override_settings(RUNTIME=root, LOCAL_STORAGE_ROOT=root / "private/storage"):
            accepted = self.send("cloud-fixture", ("p.png", photo("white")))
            self.assertEqual(accepted.status_code, 201)
            sub = Submission.objects.get()
            self.assertEqual(sub.attachments.get().file.library_id, "simulation-cloud-library")
            snapshot = self.folder / "snapshot"
            restored = self.folder / "restored"
            call_command("snapshot_teaching", snapshot)
            call_command("restore_teaching", snapshot, restored)
            self.assertEqual(
                probe(restored, restored / "school.sqlite3", INSPECT),
                {str(sub.pk): [sub.attachments.get().file.sha256]},
            )
