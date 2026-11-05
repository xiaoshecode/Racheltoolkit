from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .accounts import _digest
from .invitations import issue_invitation, join_class, register_student, revoke_invitation
from .models import ClassInvitation, Course, CourseEditor, Enrollment, TeachingAssignment, TeachingClass, Term


PASSWORD = "Classroom-Cobalt-47369"
FAST_HASHER = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def create_classroom():
    term = Term.objects.create(code="2026")
    course = Course.objects.create(code="Math", title="数学")
    group = TeachingClass.objects.create(term=term, course=course, code="A班")
    teacher = get_user_model().objects.create_user("teacher", password=PASSWORD)
    TeachingAssignment.objects.create(teacher=teacher, teaching_class=group)
    return group, teacher


@override_settings(PASSWORD_HASHERS=FAST_HASHER)
class InvitationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group, cls.teacher = create_classroom()
        cls.other_group = TeachingClass.objects.create(term=cls.group.term, course=cls.group.course, code="B班")
        cls.other_teacher = get_user_model().objects.create_user("other-teacher", password=PASSWORD)
        TeachingAssignment.objects.create(teacher=cls.other_teacher, teaching_class=cls.other_group)

    def test_issue_rotate_revoke_and_teacher_scope(self):
        code, invitation = issue_invitation(self.teacher, self.group)
        self.assertEqual(invitation.digest, _digest(code))
        self.assertNotEqual(invitation.digest, code)
        self.assertEqual(len(code), 32)
        self.assertEqual(invitation.max_uses, 30)
        self.assertEqual(invitation.uses, 0)
        self.assertEqual(invitation.created_by, self.teacher)
        self.assertAlmostEqual((invitation.expires_at - timezone.now()).total_seconds(), 7 * 86400, delta=10)
        editor = get_user_model().objects.create_user("editor", password=PASSWORD)
        CourseEditor.objects.create(course=self.group.course, teacher=editor)
        admin = get_user_model().objects.create_superuser("admin", password=PASSWORD)
        for unauthorized in (AnonymousUser(), self.other_teacher, editor, admin):
            with self.subTest(actor=str(unauthorized)):
                with self.assertRaises(ValidationError):
                    issue_invitation(unauthorized, self.group)
                with self.assertRaises(ValidationError):
                    revoke_invitation(unauthorized, invitation.pk)
        replacement, current = issue_invitation(self.teacher, self.group, valid_days=1, max_uses=1)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.revoked_at)
        with self.assertRaises(ValidationError):
            register_student(code, "0001", "学生甲", PASSWORD)
        revoke_invitation(self.teacher, current.pk)
        revoke_invitation(self.teacher, current.pk)
        with self.assertRaises(ValidationError):
            register_student(replacement, "0001", "学生甲", PASSWORD)
        self.assertFalse(get_user_model().objects.filter(username="0001").exists())

    def test_invitation_input_bounds_and_revoked_teacher(self):
        for days, limit in [(0, 30), (31, 30), (True, 30), (7, 0), (7, 501), (7, "30")]:
            with self.subTest(days=days, limit=limit), self.assertRaises(ValidationError):
                issue_invitation(self.teacher, self.group, days, limit)
        code, invitation = issue_invitation(self.teacher, self.group)
        TeachingAssignment.objects.filter(teacher=self.teacher).update(active=False)
        with self.assertRaises(ValidationError):
            issue_invitation(self.teacher, self.group)
        with self.assertRaises(ValidationError):
            register_student(code, "0001", "学生甲", PASSWORD)
        TeachingAssignment.objects.filter(teacher=self.teacher).update(active=True)
        get_user_model().objects.filter(pk=self.teacher.pk).update(is_active=False)
        # Even a stale in-memory teacher instance cannot issue or redeem invitations.
        with self.assertRaises(ValidationError):
            issue_invitation(self.teacher, self.group)
        with self.assertRaises(ValidationError):
            register_student(code, "0001", "学生甲", PASSWORD)
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 0)

    def test_registration_preserves_identifier_and_only_enrolls_as_student(self):
        code, invitation = issue_invitation(self.teacher, self.group)
        student = register_student(" " + code + " ", " ００１２ ", " 学生甲 ", PASSWORD)
        self.assertEqual(student.username, "0012")
        self.assertEqual(student.first_name, "学生甲")
        self.assertTrue(student.is_active)
        self.assertTrue(student.check_password(PASSWORD))
        self.assertFalse(student.is_staff)
        self.assertFalse(student.is_superuser)
        self.assertFalse(student.teaching_assignments.exists())
        self.assertFalse(student.course_editorships.exists())
        self.assertEqual(list(student.enrollments.values_list("teaching_class_id", flat=True)), [self.group.pk])
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 1)

    def test_existing_accounts_cannot_be_claimed_including_preimported_accounts(self):
        code, invitation = issue_invitation(self.teacher, self.group)
        User = get_user_model()
        preimported = User.objects.create_user("0001", first_name="同名学生")
        disabled = User.objects.create_user("0002", first_name="同名学生", is_active=False)
        student = User.objects.create_user("0003", first_name="同名学生", password=PASSWORD)
        staff = User.objects.create_user("0004", first_name="同名学生", is_staff=True, password=PASSWORD)
        Enrollment.objects.create(student=preimported, teaching_class=self.group)
        for account in (preimported, disabled, student, staff, self.teacher):
            password_before = account.password
            name_before = account.first_name
            with self.subTest(username=account.username), self.assertRaisesMessage(ValidationError, "账号已存在"):
                register_student(code, account.username, "同名学生", PASSWORD + "-attacker")
            account.refresh_from_db()
            self.assertEqual(account.password, password_before)
            self.assertEqual(account.first_name, name_before)
        self.assertFalse(preimported.has_usable_password())
        self.assertEqual(Enrollment.objects.count(), 1)
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 0)

    def test_invalid_inputs_and_bad_password_never_create_or_consume(self):
        code, invitation = issue_invitation(self.teacher, self.group)
        for username, name, password in [
            ("", "学生", PASSWORD), ("a b", "学生", PASSWORD), ("a" * 151, "学生", PASSWORD),
            ("0001", "", PASSWORD), ("0001", "生" * 151, PASSWORD),
            ("0001", "学生", "12345678"), ("0001", "学生", ""), ("0001", "学生", "a" * 257),
        ]:
            with self.subTest(username=username, name=name), self.assertRaises(ValidationError):
                register_student(code, username, name, password)
        self.assertFalse(Enrollment.objects.exists())
        self.assertFalse(get_user_model().objects.filter(username="0001").exists())
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 0)

    def test_invalid_expired_and_exhausted_invites_are_rejected_before_hashing(self):
        code, invitation = issue_invitation(self.teacher, self.group, max_uses=1)
        with patch("school.invitations._new_student") as create:
            with self.assertRaises(ValidationError):
                register_student("wrong", "0001", "学生", PASSWORD)
            ClassInvitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
            with self.assertRaises(ValidationError):
                register_student(code, "0001", "学生", PASSWORD)
            ClassInvitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now()+timedelta(days=1), uses=1)
            with self.assertRaises(ValidationError):
                register_student(code, "0001", "学生", PASSWORD)
            create.assert_not_called()
        self.assertFalse(Enrollment.objects.exists())

    def test_invitation_rechecked_after_password_hash(self):
        from .invitations import _new_student

        code, invitation = issue_invitation(self.teacher, self.group)

        def revoke_during_hash(*args):
            student = _new_student(*args)
            revoke_invitation(self.teacher, invitation.pk)
            return student

        with patch("school.invitations._new_student", side_effect=revoke_during_hash):
            with self.assertRaises(ValidationError):
                register_student(code, "0001", "学生", PASSWORD)
        self.assertFalse(get_user_model().objects.filter(username="0001").exists())
        self.assertFalse(Enrollment.objects.exists())
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 0)

    def test_join_second_class_is_idempotent_and_does_not_restore_retired_membership(self):
        first_code, _ = issue_invitation(self.teacher, self.group)
        student = register_student(first_code, "0001", "学生", PASSWORD)
        code, invitation = issue_invitation(self.other_teacher, self.other_group, max_uses=1)
        enrollment = join_class(student, code)
        self.assertEqual(enrollment.student, student)
        self.assertEqual(enrollment.teaching_class, self.other_group)
        self.assertEqual(join_class(student, code).pk, enrollment.pk)
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 1)
        self.assertEqual(student.enrollments.count(), 2)
        enrollment.active = False
        enrollment.save(update_fields=["active"])
        with self.assertRaisesMessage(ValidationError, "选课已停用"):
            join_class(student, code)
        enrollment.refresh_from_db()
        self.assertFalse(enrollment.active)

    def test_join_rejects_nonstudents_and_disabled_accounts(self):
        code, invitation = issue_invitation(self.teacher, self.group)
        User = get_user_model()
        staff = User.objects.create_user("staff", password=PASSWORD, is_staff=True)
        editor = User.objects.create_user("editor", password=PASSWORD)
        CourseEditor.objects.create(course=self.group.course, teacher=editor)
        disabled = User.objects.create_user("disabled", password=PASSWORD)
        User.objects.filter(pk=disabled.pk).update(is_active=False)
        preimported = User.objects.create_user("preimported")
        for account in (AnonymousUser(), self.teacher, staff, editor, disabled, preimported):
            with self.subTest(actor=str(account)), self.assertRaises(ValidationError):
                join_class(account, code)
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 0)
        self.assertFalse(Enrollment.objects.exists())


@override_settings(PASSWORD_HASHERS=FAST_HASHER)
class ConcurrentInvitationTests(TransactionTestCase):
    def setUp(self):
        self.group, self.teacher = create_classroom()

    def race(self, operations):
        barrier = Barrier(len(operations))

        def perform(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation()
            except ValidationError:
                return None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            return list(pool.map(perform, operations))

    def test_only_one_registration_claims_last_invitation_use(self):
        code, invitation = issue_invitation(self.teacher, self.group, max_uses=1)
        results = self.race([
            lambda: register_student(code, "0001", "学生甲", PASSWORD).pk,
            lambda: register_student(code, "0002", "学生乙", PASSWORD).pk,
        ])
        self.assertEqual(len([result for result in results if result is not None]), 1)
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertEqual(get_user_model().objects.filter(username__in=["0001", "0002"]).count(), 1)
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 1)

    def test_concurrent_identifier_collision_does_not_consume_second_invite(self):
        group = TeachingClass.objects.create(term=self.group.term, course=self.group.course, code="B班")
        TeachingAssignment.objects.create(teacher=self.teacher, teaching_class=group)
        first, first_invitation = issue_invitation(self.teacher, self.group, max_uses=1)
        second, second_invitation = issue_invitation(self.teacher, group, max_uses=1)
        results = self.race([
            lambda: register_student(first, "0001", "学生甲", PASSWORD).pk,
            lambda: register_student(second, "0001", "学生乙", PASSWORD + "-second").pk,
        ])
        self.assertEqual(len([result for result in results if result is not None]), 1)
        self.assertEqual(get_user_model().objects.filter(username="0001").count(), 1)
        self.assertEqual(Enrollment.objects.count(), 1)
        first_invitation.refresh_from_db()
        second_invitation.refresh_from_db()
        self.assertEqual(first_invitation.uses + second_invitation.uses, 1)

    def test_concurrent_repeat_join_consumes_one_use(self):
        student = get_user_model().objects.create_user("0001", password=PASSWORD)
        code, invitation = issue_invitation(self.teacher, self.group, max_uses=1)
        results = self.race([lambda: join_class(student, code).pk, lambda: join_class(student, code).pk])
        self.assertNotIn(None, results)
        self.assertEqual(results[0], results[1])
        self.assertEqual(Enrollment.objects.count(), 1)
        invitation.refresh_from_db()
        self.assertEqual(invitation.uses, 1)
