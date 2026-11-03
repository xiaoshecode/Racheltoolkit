"""M7a: real multipart requests, disk SQLite, concurrent app sessions and immutable bytes."""

import hashlib
import json
import threading
import time
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connections, transaction
from django.test import Client
from pypdf import PdfWriter

from grading.services import publish_grades, save_grade
from school.models import Enrollment
from submissions.models import Submission
from submissions.tests import WorkflowBase, photo
from storage.services import stream


class ConcentratedIntake(WorkflowBase):
    def test_twenty_students_and_duplicate_under_writer_lock(self):
        clients = []
        fixture = {}
        for i in range(20):
            student = get_user_model().objects.create(username=f"08{i:02}")
            Enrollment.objects.create(student=student, teaching_class=self.x)
            client = Client()
            client.force_login(student)
            if i % 4 == 0:
                writer = PdfWriter()
                writer.add_blank_page(width=240 + i, height=320)
                body = BytesIO()
                writer.write(body)
                pages = [(f"work-{i}.pdf", body.getvalue())]
            else:
                pages = [(f"page-{n}.png", photo((i + 1, n, 237))) for n in (1, 2)]
            clients.append((student.username, client, pages))
            fixture[student.username] = [hashlib.sha256(data).hexdigest() for _, data in pages]
        # Two HTTP clients replay the same authenticated key; launch while the writer is locked.
        duplicate = Client()
        duplicate.cookies = clients[0][1].cookies.copy()
        calls = clients + [(clients[0][0], duplicate, clients[0][2])]
        start = threading.Barrier(len(calls) + 1)

        def send_one(username, client, pages):
            close_old_connections()
            try:
                start.wait(15)
                response = self.send("turnin-" + username, *pages, client=client)
                return username, response.status_code
            finally:
                connections.close_all()

        began = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            with transaction.atomic():
                futures = [pool.submit(send_one, *args) for args in calls]
                start.wait(15)
                time.sleep(0.25)
            first = [f.result(timeout=35) for f in futures]
        self.assertTrue(all(s in (200, 201, 202) for _, s in first), first)
        replay = []
        for username, client, pages in clients:
            result = self.send("turnin-" + username, *pages, client=client)
            self.assertEqual(result.status_code, 200)
            replay.append(str(result.json()["receipt"]))
        self.assertEqual(len(set(replay)), 20)
        received = Submission.objects.filter(state="confirmed").select_related("student")
        self.assertEqual(received.count(), 20)
        self.assertEqual(Submission.objects.count(), 20)
        hashes = 0
        selected = {}
        for sub in received:
            actual = []
            for a in sub.attachments.select_related("file").order_by("position"):
                with stream(a.file) as src:
                    actual.append(hashlib.file_digest(src, "sha256").hexdigest())
                    hashes += 1
            self.assertEqual(actual, fixture[sub.student.username])
            draft = save_grade(self.teacher, sub.pk, 0, "16", "本次演练", None, [], [])
            selected[str(sub.pk)] = draft.edit_version
        self.assertEqual(
            publish_grades(self.teacher, self.work.pk, "twenty", selected).grades.count(), 20
        )
        self.client.force_login(self.teacher)
        roster = self.client.get(f"/assignments/{self.work.pk}/").context["roster"]
        self.assertEqual({u.username for u, s in roster if s}, set(fixture))
        self.assertEqual(
            {u.username for u, s in roster if not s}, {self.a.username, self.b.username}
        )
        print(
            "M7a "
            + json.dumps(
                {
                    "database": "disk SQLite",
                    "lock_ms": 250,
                    "first": first,
                    "same_key_replays": 20,
                    "first_failures": sum(code >= 400 for _, code in first),
                    "documents": 5,
                    "ordered_photo_sets": 15,
                    "graded": 20,
                    "receipts": len(set(replay)),
                    "verified_files": hashes,
                    "elapsed_seconds": round(time.monotonic() - began, 2),
                    "fixture": fixture,
                },
                sort_keys=True,
            )
        )
