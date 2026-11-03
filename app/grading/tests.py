from concurrent.futures import ThreadPoolExecutor
import json
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from pypdf import PdfWriter

from school.models import Course, Enrollment, TeachingAssignment, TeachingClass, Term
from storage.models import PreviewAsset, StoredFile
from storage.backends import LocalBackend
from submissions.models import Assignment, Submission, SubmissionAttachment
from .models import AnswerRelease, GradeRelease, MistakeCard, ReleasedGrade
from .services import Conflict, Invalid, publish_answers, publish_grades, save_answers, save_grade


def fixture(case):
    create = get_user_model().objects.create_user
    case.j, case.k = create("J"), create("K")
    case.a, case.b, case.c, case.left = [create(str(i).zfill(4)) for i in range(1, 5)]
    term = Term.objects.create(code="TEST")
    course = Course.objects.create(code="DEMO", title="数学")
    x, y = [TeachingClass.objects.create(term=term, course=course, code=code) for code in "XY"]
    for student, group, active in [
        (case.a, x, True),
        (case.b, x, True),
        (case.c, y, True),
        (case.left, x, False),
    ]:
        Enrollment.objects.create(student=student, teaching_class=group, active=active)
    for teacher, group in [(case.j, x), (case.k, y)]:
        TeachingAssignment.objects.create(teacher=teacher, teaching_class=group)
    now = timezone.now()
    case.task, case.later = [
        Assignment.objects.create(
            teaching_class=group,
            title="练习",
            published_at=now - timedelta(days=3),
            due_at=now + timedelta(hours=h),
            max_points=20,
        )
        for group, h in [(x, 1), (y, 5)]
    ]
    case.v1, case.attachment = received(case.task, case.a, 1)
    case.questions = [{"number": "2", "prompt": "求积", "topic": "乘法", "answer": "标准解答甲"}]
    case.key = save_answers(case.j, case.task.pk, 0, "P", 1, case.questions)
    case.entry = case.key.entries.first()
    case.mark = {
        "type": "text",
        "attachment_id": str(case.attachment.pk),
        "source_sha256": case.attachment.file.sha256,
        "renderer_version": "pages-v1",
        "page": 2,
        "width": 800,
        "height": 600,
        "rotation": 0,
        "x": 0.4,
        "y": 0.65,
        "text": "私有批注甲",
    }
    case.judgment = {
        "question_id": case.entry.answer.question_id,
        "attachment_id": str(case.attachment.pk),
        "page": 2,
        "verdict": "wrong",
        "reason": "私有错因甲",
    }


def received(task, user, version):
    now = timezone.now()
    sub = Submission.objects.create(
        assignment=task,
        student=user,
        submitted_by=user,
        source="self",
        version=version,
        retry_key=str(uuid4()),
        state="confirmed",
        admitted_at=now,
        received_at=now,
    )
    f = StoredFile.objects.create(
        kind="submission",
        library_id="sandbox",
        path=f"/data/{uuid4()}.pdf",
        original_name="作业.pdf",
        size=4,
        sha256="f" * 64,
        state="ready",
    )
    attachment = SubmissionAttachment.objects.create(submission=sub, position=1, file=f)
    PreviewAsset.objects.create(
        source_file=f,
        source_sha256=f.sha256,
        output_file=f,
        state="ready",
        renderer_version="pages-v1",
        page_manifest=[{"page": p, "width": 800, "height": 600, "rotation": 0} for p in [1, 2]],
    )
    return sub, attachment


class GradingTests(TestCase):
    def setUp(self):
        fixture(self)

    def draft(self, sub=None, score="0", version=0, marks=None, judgments=None, key=True):
        return save_grade(
            self.j,
            (sub or self.v1).pk,
            version,
            score,
            "继续练习",
            self.key.pk if key else None,
            marks if marks is not None else [self.mark],
            judgments if judgments is not None else [self.judgment],
        )

    def release(self, draft, retry):
        return publish_grades(self.j, self.task.pk, retry, {str(draft.submission_id): draft.edit_version})

    def answers(self):
        Assignment.objects.filter(pk=self.task.pk).update(due_at=timezone.now() - timedelta(seconds=1))
        return publish_answers(self.j, self.task.pk, self.key.pk, "答案发布")

    def test_t6_replay_old_grade_visible_changed_draft_conflicts(self):
        first = self.draft()
        batch = self.release(first, "retry")
        self.assertEqual(self.release(first, "retry").pk, batch.pk)
        self.assertEqual(GradeRelease.objects.count(), 1)
        v2, _ = received(self.task, self.a, 2)
        second = self.draft(v2, score="16", marks=[], judgments=[])
        self.client.force_login(self.a)
        self.assertContains(self.client.get("/me/grades/"), "0.00")
        self.assertNotContains(self.client.get("/me/grades/"), "16.00")
        self.assertContains(self.client.get("/me/grades/"), "V2 已收件，成绩待发布")
        self.client.force_login(self.b)
        self.assertContains(self.client.get("/me/grades/"), "未交")
        self.assertNotContains(self.client.get("/me/grades/"), "0.00")
        self.client.force_login(self.a)
        with self.assertRaises(Conflict):
            self.release(second, "retry")
        self.draft(v2, "17", 1, [], [])
        with self.assertRaises(Conflict):
            self.release(second, "stale")
        second.refresh_from_db()
        self.release(second, "new")
        self.assertContains(self.client.get("/me/grades/"), "17.00")
        self.assertEqual(
            list(ReleasedGrade.objects.order_by("pk").values_list("score", flat=True)),
            [Decimal(0), Decimal(17)],
        )

    def test_t7_t14_grade_detail_and_solution_layers_have_separate_gates(self):
        snapshot = self.release(self.draft(), "g").grades.get()
        url = f"/me/grades/{snapshot.pk}/"
        self.assertEqual(self.client.get(url).status_code, 302)
        for user in (self.b, self.c, self.left):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.a)
        self.assertNotContains(self.client.get(url), json.dumps("私有批注甲"))
        self.assertNotContains(self.client.get(url), 'id="annotation-data"')
        self.assertNotContains(self.client.get("/me/mistakes/"), "私有错因甲")
        self.answers()
        self.assertContains(self.client.get(url), json.dumps("私有批注甲"))
        self.assertContains(self.client.get("/me/mistakes/"), "私有错因甲")
        self.client.force_login(self.b)
        self.assertContains(self.client.get(f"/assignments/{self.task.pk}/answer-key/"), "标准解答甲")
        self.assertNotContains(self.client.get("/me/mistakes/"), "私有错因甲")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_t14_t15_anchors_conflicts_and_wrong_paper_blocked(self):
        first = self.draft()
        with self.assertRaises(Conflict):
            self.draft(version=0)
        other, other_attachment = received(self.task, self.a, 2)
        for mark in [
            dict(self.mark, page=3),
            dict(self.mark, x=-0.01),
            dict(self.mark, source_sha256="a" * 64),
            dict(self.mark, attachment_id=str(other_attachment.pk)),
            dict(self.mark, rotation=90),
        ]:
            with self.assertRaises(Invalid):
                self.draft(version=1, marks=[mark])
        q = save_answers(self.j, self.task.pk, 1, "Q", 1, [dict(self.questions[0], prompt="另一题")])
        with self.assertRaises(Invalid):
            self.draft(
                version=1, judgments=[dict(self.judgment, question_id=q.entries.first().answer.question_id)]
            )
        self.assertEqual(first.edit_version, 1)
        self.assertFalse(hasattr(other, "grade_draft"))

    def test_t15_card_history_republish_correct_verdict_and_review_scope(self):
        first = self.draft()
        self.release(first, "one")
        self.assertEqual(MistakeCard.objects.filter(active=True).count(), 1)
        self.answers()
        card = MistakeCard.objects.get()
        url = f"/me/mistakes/{card.pk}/review/"
        self.client.force_login(self.b)
        self.assertEqual(self.client.post(url, {"reviewed": "1"}).status_code, 404)
        self.client.force_login(self.a)
        self.assertEqual(self.client.post(url, {"reviewed": "1"}).status_code, 302)
        card.refresh_from_db()
        self.assertTrue(card.reviewed)
        correction = save_answers(self.j, self.task.pk, 1, "P", 1, [dict(self.questions[0], answer="更正版")])
        publish_answers(self.j, self.task.pk, correction.pk, "更正")
        self.assertEqual(card.answer.text, "标准解答甲")
        corrected = self.draft(version=1, judgments=[dict(self.judgment, verdict="correct")])
        self.release(corrected, "two")
        self.assertEqual(MistakeCard.objects.filter(active=True).count(), 0)
        self.assertEqual(ReleasedGrade.objects.count(), 2)

    def test_t16_exact_cutoff_and_grade_still_draft(self):
        self.draft()
        with self.assertRaises(Conflict):
            publish_answers(self.j, self.task.pk, self.key.pk, "early")
        now = timezone.now()
        Assignment.objects.filter(pk=self.task.pk).update(due_at=now)
        url = f"/assignments/{self.task.pk}/answer-key/"
        for user in (self.a, self.b):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(url), "标准解答甲")
        release = publish_answers(self.j, self.task.pk, self.key.pk, "ok")
        self.assertEqual(publish_answers(self.j, self.task.pk, self.key.pk, "ok").pk, release.pk)
        for user in (self.a, self.b):
            self.client.force_login(user)
            self.assertContains(self.client.get(url), "标准解答甲")
            self.assertNotContains(self.client.get("/me/mistakes/"), "私有错因甲")
        self.client.force_login(self.c)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(AnswerRelease.objects.count(), 1)

    def test_same_paper_two_classes_and_exact_deadline_order(self):
        y_key = save_answers(self.k, self.later.pk, 0, "P", 1, self.questions)
        self.assertEqual(y_key.paper_id, self.key.paper_id)
        self.client.force_login(self.c)
        y_url = f"/assignments/{self.later.pk}/answer-key/"
        self.assertNotContains(self.client.get(y_url), "标准解答甲")
        with patch(
            "grading.services.timezone.now", return_value=self.task.due_at - timedelta(microseconds=1)
        ):
            with self.assertRaises(Conflict):
                publish_answers(self.j, self.task.pk, self.key.pk, "x-release")
        with patch("grading.services.timezone.now", return_value=self.task.due_at):
            x_release = publish_answers(self.j, self.task.pk, self.key.pk, "x-release")
            self.assertEqual(x_release.released_at, x_release.due_at)
            with self.assertRaises(Conflict):
                publish_answers(self.k, self.later.pk, y_key.pk, "y-release")
        self.assertNotContains(self.client.get(y_url), "标准解答甲")
        self.assertEqual(self.client.get(f"/assignments/{self.task.pk}/answer-key/").status_code, 404)

    def test_answer_attachment_and_preview_follow_only_this_class_release(self):
        with (
            TemporaryDirectory(dir=settings.RUNTIME) as temp,
            patch("storage.services.get_backend", return_value=LocalBackend(Path(temp))),
        ):
            buffer = BytesIO()
            writer = PdfWriter()
            writer.add_blank_page(width=400, height=600)
            writer.write(buffer)
            pdf = buffer.getvalue()
            self.client.force_login(self.j)
            form_url = f"/teacher/assignments/{self.task.pk}/answer-key/"
            form = {
                "action": "save",
                "version": "1",
                "paper": "P",
                "paper_version": "1",
                "number": ["2"],
                "prompt": ["求积"],
                "topic": ["乘法"],
                "answer": ["乙版答案"],
                "file": SimpleUploadedFile("答案.pdf", pdf, content_type="application/pdf"),
            }
            self.assertEqual(self.client.post(form_url, form).status_code, 302)
            revision = self.task.answer_revisions.get(seq=2)
            publish = {"action": "publish", "revision": revision.pk, "request_key": "post-publish"}
            self.assertEqual(self.client.post(form_url, publish).status_code, 409)
            read = f"/assignments/{self.task.pk}/answer-key/{revision.pk}/"
            self.client.force_login(self.b)
            for action in ("download", "preview", "preview-status"):
                self.assertEqual(self.client.get(read + action + "/").status_code, 404)
            Assignment.objects.filter(pk=self.task.pk).update(due_at=timezone.now() - timedelta(seconds=1))
            self.assertEqual(self.client.get(read + "download/").status_code, 404)
            self.client.force_login(self.j)
            self.assertEqual(self.client.post(form_url, publish).status_code, 302)
            for user in (self.a, self.b):
                self.client.force_login(user)
                self.assertContains(self.client.get(f"/assignments/{self.task.pk}/answer-key/"), "乙版答案")
                for action in ("preview", "download"):
                    response = self.client.get(read + action + "/")
                    self.assertEqual(b"".join(response.streaming_content), pdf)
                    self.assertIn("no-store", response["Cache-Control"])
            for outsider in (self.c, self.left, self.k):
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(read + "preview/").status_code, 404)
                self.assertEqual(self.client.get(form_url).status_code, 404)

    def test_http_save_preview_confirm_and_unguided_score_never_infers_mistakes(self):
        teacher = f"/teacher/submissions/{self.v1.pk}/review/"
        publish = f"/teacher/assignments/{self.task.pk}/grades/"
        self.client.force_login(self.k)
        self.assertEqual(self.client.get(teacher).status_code, 404)
        self.assertEqual(self.client.get(publish).status_code, 404)
        self.client.force_login(self.j)
        self.assertContains(self.client.get(teacher), "判定与评分")
        form = {
            "version": "0",
            "score": "0",
            "feedback": "有待复习",
            "annotations": "[]",
            "key_id": str(self.key.pk),
        }
        self.assertEqual(self.client.post(teacher, form).status_code, 302)
        self.assertEqual(self.client.post(teacher, form).status_code, 409)
        self.assertContains(self.client.get(publish), "草稿 R1")
        selected = {"draft": [f"{self.v1.pk}:1"], "request_key": "http-release"}
        self.assertContains(self.client.post(publish, selected), "共 1 人")
        self.assertContains(self.client.post(publish, selected), "共 1 人")
        self.assertEqual(GradeRelease.objects.count(), 1)
        self.assertFalse(MistakeCard.objects.exists())

    def test_failed_multi_student_release_rolls_back_and_snapshot_scope_survives_regrading(self):
        draft = self.draft()
        bv1, _ = received(self.task, self.b, 1)
        bdraft = save_grade(self.j, bv1.pk, 0, "16", "", None, [], [])
        choices = {str(self.v1.pk): draft.edit_version, str(bv1.pk): bdraft.edit_version + 1}
        with self.assertRaises(Conflict):
            publish_grades(self.j, self.task.pk, "preview-stale", choices)
        self.assertEqual(GradeRelease.objects.count(), 0)
        self.assertFalse(MistakeCard.objects.exists())
        first = self.release(draft, "valid").grades.get()
        self.draft(version=1, score="12")
        audit = f"/teacher/grades/{first.pk}/"
        for account in (self.a, self.b, self.k):
            self.client.force_login(account)
            self.assertEqual(self.client.get(audit).status_code, 404)
        self.client.force_login(self.j)
        self.assertContains(self.client.get(audit), "0.00/20")
        self.assertContains(self.client.get(audit), json.dumps(self.mark["text"]))

    def test_grade_for_unpublished_key_never_reveals_its_solution_after_a_different_release(self):
        grade = self.release(self.draft(), "first").grades.get()
        correction = save_answers(
            self.j, self.task.pk, 1, "P", 1, [dict(self.questions[0], answer="修订解法")]
        )
        Assignment.objects.filter(pk=self.task.pk).update(due_at=timezone.now() - timedelta(seconds=1))
        publish_answers(self.j, self.task.pk, correction.pk, "latest")
        self.client.force_login(self.a)
        self.assertNotContains(self.client.get(f"/me/grades/{grade.pk}/"), 'id="annotation-data"')
        self.assertNotContains(self.client.get("/me/mistakes/"), self.judgment["reason"])
        self.client.force_login(self.j)
        review_url = f"/teacher/submissions/{self.v1.pk}/review/"
        self.assertContains(
            self.client.get(review_url + f"?key={correction.pk}"), f'name="key_id" value="{correction.pk}"'
        )
        self.assertNotContains(
            self.client.get(review_url + f"?key={correction.pk}"), '<option value="wrong" selected'
        )
        revised = save_grade(
            self.j, self.v1.pk, 1, "10", "继续练习", correction.pk, [self.mark], [self.judgment]
        )
        second = self.release(revised, "confirmed-correction").grades.get()
        self.client.force_login(self.a)
        self.assertContains(self.client.get(f"/me/grades/{second.pk}/"), json.dumps(self.mark["text"]))
        self.assertContains(self.client.get("/me/mistakes/"), "修订解法")

    def test_malformed_grading_requests_return_recoverable_client_errors(self):
        self.client.force_login(self.j)
        url = f"/teacher/submissions/{self.v1.pk}/review/"
        form = {"version": "0", "score": "12", "feedback": "保留本次输入", "key_id": self.key.pk}
        for bad in ("[", "[" * 1100 + "]" * 1100, json.dumps([dict(self.mark, x=10**400)])):
            response = self.client.post(url, {**form, "annotations": bad})
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, "保留本次输入", status_code=400)
        self.assertFalse(hasattr(self.v1, "grade_draft"))


class DeadlineRaceTests(TransactionTestCase):
    def test_publish_or_extend_has_one_winner_across_independent_connections(self):
        from submissions.services import Refused, extend

        fixture(self)
        now = timezone.now()
        Assignment.objects.filter(pk=self.task.pk).update(due_at=now - timedelta(seconds=1))
        barrier = Barrier(2)

        def run(action):
            try:
                barrier.wait(timeout=8)
                if action == "publish":
                    publish_answers(self.j, self.task.pk, self.key.pk, "race")
                else:
                    extend(self.task.pk, self.j, now + timedelta(hours=3), "教学安排调整")
                return action
            except (Conflict, Refused):
                return None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            won = [value for value in pool.map(run, ["extend", "publish"]) if value]
        self.assertEqual(len(won), 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.answer_releases.count(), int(won == ["publish"]))
        self.assertEqual(self.task.due_at > now, won == ["extend"])
