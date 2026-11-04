from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from .accounts import issue_code, redeem_code
from .models import ActivationOrReset, Course, CourseEditor, Enrollment, TeachingAssignment, TeachingClass, Term
from .permissions import editable_courses, student_classes, teacher_classes
from .roster import check_roster, import_roster


PASSWORD = "Classroom-Cobalt-47369"


class IdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        term = Term.objects.create(code="2026")
        cls.course = Course.objects.create(code="Math", title="数学")
        cls.x = TeachingClass.objects.create(term=term, course=cls.course, code="X班")
        cls.y = TeachingClass.objects.create(term=term, course=cls.course, code="Y班")
        cls.teacher = get_user_model().objects.create_user("J", first_name="吉老师")
        TeachingAssignment.objects.create(teacher=cls.teacher, teaching_class=cls.x)
        cls.csv = f"student_id,name,class_id\n0001,同名学生,{cls.x.pk}\n0002,同名学生,{cls.x.pk}"

    def test_import_preflight_idempotent_conflict_and_rollback(self):
        self.assertEqual(len(check_roster(self.csv)), 2)
        self.assertEqual(Enrollment.objects.count(), 0)
        self.assertEqual(import_roster(self.csv), (2, 2))
        self.assertEqual(import_roster(self.csv), (0, 0))
        for extra in (f"0001,冲突姓名,{self.x.pk}", "0008,有效姓名,999", f"0002,同名学生,{self.x.pk}"):
            with self.assertRaises(ValidationError):
                import_roster(f"{self.csv}\n0007,新学生,{self.x.pk}\n{extra}")
        self.assertEqual(Enrollment.objects.count(), 2)
        self.assertFalse(get_user_model().objects.filter(username="0007").exists())
        self.assertFalse(get_user_model().objects.get(username="0001").has_usable_password())

    def test_teacher_preview_and_confirm_bound_to_teacher_scope_and_payload(self):
        self.client.force_login(self.teacher)
        url = "/teacher/roster/"
        self.assertContains(self.client.post(url, {"contents": self.csv, "action": "preview"}), "预检通过")
        self.assertEqual(Enrollment.objects.count(), 0)
        changed = self.csv.replace(str(self.x.pk), str(self.y.pk))
        self.assertContains(self.client.post(url, {"contents": changed, "action": "apply"}), "重新预检")
        self.assertContains(self.client.post(url, {"contents": self.csv, "action": "apply"}), "新增 2 个账号")
        self.assertContains(self.client.post(url, {"contents": f"student_id,name,class_id\n0003,外班,{self.y.pk}",
            "action": "preview"}), "无权管理")
        self.assertEqual(Enrollment.objects.count(), 2)
        self.client.force_login(get_user_model().objects.get(username="0002"))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_class_and_course_scopes_do_not_imply_each_other(self):
        editor = get_user_model().objects.create_user("Editor")
        CourseEditor.objects.create(course=self.course, teacher=editor)
        self.assertEqual(list(teacher_classes(self.teacher)), [self.x])
        self.assertFalse(editable_courses(self.teacher).exists())
        self.assertEqual(list(editable_courses(editor)), [self.course])
        self.assertFalse(teacher_classes(editor).exists())
        self.assertFalse(student_classes(AnonymousUser()).exists())

    def test_workspace_navigation_follows_current_independent_roles(self):
        editor = get_user_model().objects.create_user("Editor-nav")
        CourseEditor.objects.create(course=self.course, teacher=editor)
        self.client.force_login(editor)
        response = self.client.get("/me/")
        self.assertContains(response, 'href="/teacher/courses/"')
        self.assertNotContains(response, 'href="/dashboard/"')
        self.assertNotContains(response, 'href="/assignments/"')
        self.client.force_login(self.teacher)
        response = self.client.get("/dashboard/")
        self.assertContains(response, 'href="/dashboard/" aria-current="page"')
        self.assertNotContains(response, 'href="/teacher/courses/"')
        TeachingAssignment.objects.filter(teacher=self.teacher).update(active=False)
        response = self.client.get("/me/")
        self.assertNotContains(response, 'href="/dashboard/"')
        self.assertNotContains(response, 'href="/teacher/roster/"')
        self.client.logout()
        self.assertNotContains(self.client.get("/"), 'aria-label="工作区导航"')

    def test_two_users_same_browser_activate_login_me_logout(self):
        import_roster(self.csv)
        self.assertRedirects(self.client.get("/me/"), "/login/?next=/me/")
        for username, other in [("0001", "0002"), ("0002", "0001")]:
            user = get_user_model().objects.get(username=username)
            code = issue_code(user, "activate")
            self.assertNotEqual(ActivationOrReset.objects.get(user=user).digest, code)
            self.assertEqual(self.client.post("/activate/", {
                "username": username, "code": code, "password": PASSWORD, "confirmation": PASSWORD,
            }).status_code, 302)
            self.assertEqual(self.client.post("/login/", {"username": username, "password": PASSWORD}).status_code, 302)
            response = self.client.get("/me/", {"student": other})
            self.assertContains(response, username)
            self.assertNotContains(response, other)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertEqual(self.client.post("/logout/").status_code, 302)
            self.assertEqual(self.client.get("/me/").status_code, 302)

    def test_retired_membership_reset_and_disable_block_old_sessions(self):
        account = get_user_model().objects.create_user("0005", password=PASSWORD)
        member = Enrollment.objects.create(teaching_class=self.x, student=account)
        self.client.force_login(account)
        self.assertContains(self.client.get("/me/"), "X班")
        member.active = False
        member.save()
        self.assertNotContains(self.client.get("/me/"), "X班")
        redeem_code(account.username, "reset", issue_code(account, "reset"), PASSWORD + "-new")
        self.assertEqual(self.client.get("/me/").status_code, 302)
        account.refresh_from_db()
        self.client.force_login(account)
        code = issue_code(account, "reset")
        account.is_active = False
        account.save(update_fields=["is_active"])
        self.assertEqual(self.client.get("/me/").status_code, 302)
        account.is_active = True
        account.save(update_fields=["is_active"])
        self.assertEqual(self.client.get("/me/").status_code, 302)
        with self.assertRaises(ValidationError):
            redeem_code(account.username, "reset", code, PASSWORD)

    def test_expiry_supersession_wrong_purpose_csrf(self):
        account = get_user_model().objects.create_user("0008")
        old, code = issue_code(account, "activate"), issue_code(account, "activate")
        for purpose, key in [("activate", old), ("reset", code)]:
            with self.assertRaises(ValidationError):
                redeem_code("0008", purpose, key, PASSWORD)
        ActivationOrReset.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            redeem_code("0008", "activate", code, PASSWORD)
        csrf = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf.post("/activate/", {"username": "0008"}).status_code, 403)
        self.assertEqual(csrf.post("/login/", {}).status_code, 403)


class ConcurrentActivationTests(TransactionTestCase):
    def test_only_one_concurrent_activation_commits(self):
        account = get_user_model().objects.create_user("0099")
        code = issue_code(account, "activate")
        barrier = Barrier(2)

        def activate(password):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                redeem_code("0099", "activate", code, password)
                return password
            except ValidationError:
                return None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(activate, [PASSWORD + "-A", PASSWORD + "-B"]))
        winners = [password for password in results if password]
        self.assertEqual(len(winners), 1)
        account.refresh_from_db()
        self.assertTrue(account.check_password(winners[0]))
        self.assertEqual(account.account_codes.filter(used_at__isnull=False).count(), 1)
