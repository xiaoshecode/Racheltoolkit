from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from .invitations import issue_invitation
from .models import ClassInvitation, Course, Enrollment, TeachingAssignment, TeachingClass, Term


PASSWORD = "Classroom-Cobalt-47369"


class RegistrationViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        term = Term.objects.create(code="signup-view-2026")
        course = Course.objects.create(code="signup-view-math", title="注册测试数学")
        cls.group = TeachingClass.objects.create(term=term, course=course, code="甲班")
        cls.other_group = TeachingClass.objects.create(term=term, course=course, code="乙班")
        cls.teacher = User.objects.create_user("signup-teacher", first_name="甲老师")
        cls.colleague = User.objects.create_user("signup-colleague", first_name="共同任课老师")
        cls.other_teacher = User.objects.create_user("signup-other-teacher", first_name="乙老师")
        TeachingAssignment.objects.create(teacher=cls.teacher, teaching_class=cls.group)
        TeachingAssignment.objects.create(teacher=cls.colleague, teaching_class=cls.group)
        TeachingAssignment.objects.create(teacher=cls.other_teacher, teaching_class=cls.other_group)
        cls.student = User.objects.create_user("00100", first_name="已有学生", password=PASSWORD)
        cls.code, cls.invitation = issue_invitation(cls.teacher, cls.group)
        cls.other_code, cls.other_invitation = issue_invitation(cls.other_teacher, cls.other_group)

    def registration_payload(self, **overrides):
        return {"code": self.code, "username": "00023", "name": "新学生",
            "password": PASSWORD, "confirmation": PASSWORD, **overrides}

    def issue_payload(self, **overrides):
        return {"action": "issue", "teaching_class": self.group.pk,
            "valid_days": 7, "max_uses": 30, **overrides}

    def test_registration_then_login_enters_class_without_granting_teacher_privileges(self):
        response = self.client.post("/register/", self.registration_payload(
            is_staff="1", is_superuser="1", teaching_class=self.other_group.pk), follow=True)
        self.assertRedirects(response, "/login/")
        self.assertContains(response, "注册成功，已加入班级")
        student = get_user_model().objects.get(username="00023")
        self.assertEqual(student.first_name, "新学生")
        self.assertTrue(student.check_password(PASSWORD))
        self.assertFalse(student.is_staff)
        self.assertFalse(student.is_superuser)
        self.assertFalse(student.teaching_assignments.exists())
        self.assertFalse(student.course_editorships.exists())
        self.assertEqual(list(student.enrollments.values_list("teaching_class_id", "active")),
            [(self.group.pk, True)])
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.uses, 1)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertRedirects(self.client.post("/login/", {"username": "00023", "password": PASSWORD}), "/me/")
        self.assertContains(self.client.get("/me/"), "甲班")
        self.assertEqual(self.client.get("/teacher/invitations/").status_code, 404)

    def test_existing_imported_inactive_and_teacher_accounts_cannot_be_claimed(self):
        User = get_user_model()
        imported = User.objects.create_user("00024", first_name="名册学生")
        inactive = User.objects.create_user("00025", first_name="停用学生", password=PASSWORD, is_active=False)
        for existing in (self.student, imported, inactive, self.teacher):
            with self.subTest(username=existing.username):
                previous = (existing.password, existing.first_name, existing.is_active,
                    existing.is_staff, existing.is_superuser)
                response = self.client.post("/register/", self.registration_payload(username=existing.username))
                self.assertContains(response, "该账号已存在")
                existing.refresh_from_db()
                self.assertEqual((existing.password, existing.first_name, existing.is_active,
                    existing.is_staff, existing.is_superuser), previous)
                self.assertFalse(existing.enrollments.exists())
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.uses, 0)

    def test_bad_confirmation_or_invitation_does_not_create_account_or_echo_password(self):
        for values, message in (({"confirmation": "Not-the-same-password"}, "两次密码输入不一致"),
                                ({"code": "not-a-real-invitation"}, "邀请码无效")):
            with self.subTest(values=values):
                response = self.client.post("/register/", self.registration_payload(**values))
                self.assertContains(response, message)
                self.assertNotContains(response, PASSWORD)
                self.assertIn("no-store", response["Cache-Control"])
                self.assertFalse(get_user_model().objects.filter(username="00023").exists())
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.uses, 0)

    def test_authenticated_registration_routes_to_join_and_preserves_identity(self):
        self.assertRedirects(self.client.get("/join/"), "/login/?next=/join/")
        self.client.force_login(self.student)
        self.assertRedirects(self.client.get("/register/"), "/join/")
        self.assertRedirects(self.client.post("/register/", self.registration_payload()), "/join/")
        self.assertFalse(get_user_model().objects.filter(username="00023").exists())
        self.assertRedirects(self.client.post("/join/", {"code": self.code}), "/me/")
        self.assertRedirects(self.client.post("/join/", {"code": self.code}), "/me/")
        self.assertEqual(self.student.enrollments.count(), 1)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.uses, 1)
        self.assertRedirects(self.client.post("/join/", {"code": self.other_code}), "/me/")
        self.assertEqual(self.student.enrollments.count(), 2)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_staff or self.student.is_superuser)

    def test_teacher_account_cannot_join_as_student(self):
        self.client.force_login(self.teacher)
        self.assertContains(self.client.post("/join/", {"code": self.code}), "有效的学生账号")
        self.assertFalse(self.teacher.enrollments.exists())

    def test_teacher_form_and_record_list_cannot_target_another_class(self):
        self.client.force_login(self.teacher)
        response = self.client.get("/teacher/invitations/")
        self.assertContains(response, "甲班")
        self.assertNotContains(response, "乙班")
        self.assertNotContains(response, self.other_invitation.digest)
        old_count = ClassInvitation.objects.count()
        response = self.client.post("/teacher/invitations/", self.issue_payload(
            teaching_class=self.other_group.pk))
        self.assertTrue(response.context["form"].errors.get("teaching_class"))
        self.assertEqual(ClassInvitation.objects.count(), old_count)
        response = self.client.post("/teacher/invitations/", {
            "action": "revoke", "invitation": self.other_invitation.pk})
        self.assertContains(response, "无权管理")
        self.other_invitation.refresh_from_db()
        self.assertIsNone(self.other_invitation.revoked_at)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get("/teacher/invitations/").status_code, 404)
        self.assertEqual(self.client.post("/teacher/invitations/", self.issue_payload()).status_code, 404)

    def test_issued_code_appears_once_and_get_parameters_cannot_issue_or_recover_it(self):
        self.client.force_login(self.teacher)
        count = ClassInvitation.objects.count()
        response = self.client.get("/teacher/invitations/", self.issue_payload())
        self.assertEqual(ClassInvitation.objects.count(), count)
        self.assertNotContains(response, self.code)
        response = self.client.post("/teacher/invitations/", self.issue_payload())
        code = response.context["code"]
        self.assertTrue(code)
        self.assertContains(response, code)
        self.assertContains(response, "仅在本次页面显示")
        self.assertIn("no-store", response["Cache-Control"])
        self.invitation.refresh_from_db()
        self.assertIsNotNone(self.invitation.revoked_at)
        latest = ClassInvitation.objects.latest("pk")
        self.assertNotEqual(latest.digest, code)
        self.assertNotContains(self.client.get("/teacher/invitations/", {"code": code}), code)
        self.client.logout()
        for path in ("/register/", "/login/"):
            self.assertNotContains(self.client.get(path, {"code": code}), code)

    def test_same_class_colleague_can_revoke_without_affecting_enrolled_students(self):
        Enrollment.objects.create(student=self.student, teaching_class=self.group)
        self.client.force_login(self.colleague)
        self.assertRedirects(self.client.post("/teacher/invitations/", {
            "action": "revoke", "invitation": self.invitation.pk}), "/teacher/invitations/")
        self.invitation.refresh_from_db()
        self.assertIsNotNone(self.invitation.revoked_at)
        self.assertTrue(self.student.enrollments.get(teaching_class=self.group).active)
        self.client.logout()
        self.assertContains(self.client.post("/register/", self.registration_payload()), "邀请码无效")
        self.assertFalse(get_user_model().objects.filter(username="00023").exists())

    def test_mutating_endpoints_require_csrf_and_valid_tokens_allow_the_intended_action(self):
        csrf = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf.post("/register/", self.registration_payload()).status_code, 403)
        csrf.get("/register/")
        token = csrf.cookies["csrftoken"].value
        response = csrf.post("/register/", self.registration_payload(), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        csrf.force_login(self.student)
        self.assertEqual(csrf.post("/join/", {"code": self.code}).status_code, 403)
        csrf.get("/join/")
        token = csrf.cookies["csrftoken"].value
        self.assertEqual(csrf.post("/join/", {"code": self.code}, HTTP_X_CSRFTOKEN=token).status_code, 302)
        csrf.force_login(self.teacher)
        for payload in (self.issue_payload(), {"action": "revoke", "invitation": self.invitation.pk}):
            self.assertEqual(csrf.post("/teacher/invitations/", payload).status_code, 403)
        csrf.get("/teacher/invitations/")
        token = csrf.cookies["csrftoken"].value
        self.assertEqual(csrf.post("/teacher/invitations/", self.issue_payload(),
            HTTP_X_CSRFTOKEN=token).status_code, 200)
        latest = ClassInvitation.objects.latest("pk")
        self.assertEqual(csrf.post("/teacher/invitations/", {"action": "revoke", "invitation": latest.pk},
            HTTP_X_CSRFTOKEN=token).status_code, 302)

    def test_help_signup_and_role_scoped_navigation_are_discoverable(self):
        self.assertContains(self.client.get("/login/"), 'href="/register/"')
        self.assertContains(self.client.get("/"), 'href="/help/"')
        self.assertContains(self.client.get("/help/"), "上传资料")
        self.client.force_login(self.teacher)
        response = self.client.get("/me/")
        self.assertContains(response, 'href="/teacher/invitations/"')
        self.assertNotContains(response, 'href="/join/"')
        self.client.force_login(self.student)
        response = self.client.get("/me/")
        self.assertContains(response, 'href="/join/"')
        self.assertNotContains(response, 'href="/teacher/invitations/"')
