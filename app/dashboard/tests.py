import csv
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from grading.models import GradeDraft, GradeRelease, ReleasedGrade
from school.models import Course, Enrollment, TeachingAssignment, TeachingClass, Term
from submissions.models import Assignment, Submission


def report_fixture():
    term = Term.objects.create(code="2026 秋")
    course = Course.objects.create(title="信息素养",code="INFO")
    x, y = [TeachingClass.objects.create(term=term,course=course,code=c) for c in ["教学一班","教学二班"]]
    people = {}
    for name, label in [("J","任课教师"),("K","其他教师"),("001","陈同学"),("002","陈同学"),
                        ("003","林同学"),("004","陈同学")]:
        people[name] = get_user_model().objects.create_user(username=name,first_name=label)
    for name, group in [("001",x),("002",x),("003",x),("004",y)]:
        Enrollment.objects.create(student=people[name],teaching_class=group)
    for name, group in [("J",x),("K",y)]:
        TeachingAssignment.objects.create(teacher=people[name],teaching_class=group)
    now = timezone.now()
    tasks = [Assignment.objects.create(teaching_class=x,title=title,published_at=now-timedelta(days=4),
            due_at=now+timedelta(days=2),max_points=points) for title,points in
            [("信息来源辨析",20),("检索练习",40),("课堂观察",10),("阅读记录",20)]]
    batch = GradeRelease.objects.create(assignment=tasks[0],publisher=people["J"],request_key="initial",fingerprint="a"*64)
    for i, name in enumerate(["001","002","003"]):
        sub = Submission.objects.create(student=people[name], assignment=tasks[0], submitted_by=people[name],
            source="self",version=1,state="confirmed",admitted_at=now,received_at=now,retry_key="initial")
        if i<2:
            ReleasedGrade.objects.create(release=batch, student=people[name], submission=sub, draft_version=1,
                                         score=[16,0][i],max_points=20,feedback="这是已发布的评语")
    sub = Submission.objects.create(student=people["001"], assignment=tasks[0], submitted_by=people["001"],
            source="self",version=2,state="confirmed",admitted_at=now,received_at=now,retry_key="second")
    GradeDraft.objects.create(submission=sub,grader=people["J"],edit_version=1,score=19,feedback="未发布草稿评语")
    return people,x,y,tasks


class GradebookTests(TestCase):
    def setUp(self):
        self.people,self.x,self.y,self.tasks = report_fixture()
        self.client.force_login(self.people["J"])

    def test_matrix_average_version_and_filtered_csv(self):
        data = {"class":self.x.pk,"q":"陈同学"}
        result = self.client.get("/dashboard/",data)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.context["page"].paginator.count,2)
        self.assertContains(result,"8/20 · 40%")
        self.assertContains(result,"计入 2 人")
        self.assertContains(result,"暂无已发布成绩")
        self.assertContains(result,"公布 16/20 · V1")
        self.assertContains(result,"公布 0/20 · V1")
        self.assertContains(result,"草稿待发布")
        self.assertNotContains(result,"未发布草稿评语")
        self.assertNotContains(result,"004</small>")
        data.update(q="001",assignment=self.tasks[0].pk,status="draft")
        result = self.client.get("/teacher/students/",data)
        self.assertEqual(result.context["page"].paginator.count,1)
        self.assertContains(result,"8/20 · 40%")
        csv_data=self.client.get("/teacher/gradebook.csv",data)
        rows=list(csv.reader(StringIO(csv_data.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1][1:9],["001","陈同学","信息来源辨析","草稿待发布","2","1","16","20"])
        detail=self.client.get(f"/teacher/students/{self.people['001'].pk}/",data)
        self.assertContains(detail,"返回成绩册筛选位置")
        self.assertContains(detail,"V2 回执")

    def test_wrong_class_rejected_including_direct_urls(self):
        for username in ["001","K"]:
            self.client.force_login(self.people[username])
            for url in ["/dashboard/","/teacher/students/",f"/teacher/students/{self.people['001'].pk}/",
                        "/teacher/gradebook.csv"]:
                self.assertEqual(self.client.get(url,{"class":self.x.pk}).status_code,404)
        self.client.force_login(self.people["J"])
        self.assertEqual(self.client.get(f"/teacher/students/{self.people['004'].pk}/",{"class":self.x.pk}).status_code,404)

    def test_published_score_bands_keep_zero_and_previous_release(self):
        result = self.client.get("/dashboard/", {"class": self.x.pk, "assignment": self.tasks[0].pk})
        cells = {row["student"].username: row["cells"][0] for row in result.context["rows"]}
        self.assertEqual((cells["001"]["status"], cells["001"]["score"], cells["001"]["score_band"]),
                         ("draft", "16", 3))
        self.assertEqual((cells["002"]["score"], cells["002"]["score_percent"], cells["002"]["score_band"]),
                         ("0", "0", 0))
        self.assertIsNone(cells["003"]["score_band"])
        self.assertIsNone(cells["003"]["score_percent"])
        grade = ReleasedGrade.objects.get(student=self.people["002"])
        for score, band in [("4.99", 0), ("5", 1), ("9.99", 1), ("10", 2), ("14.99", 2), ("15", 3), ("20", 3)]:
            with self.subTest(score=score):
                grade.score = Decimal(score)
                grade.save(update_fields=["score"])
                page = self.client.get("/dashboard/", {"class": self.x.pk, "assignment": self.tasks[0].pk, "q": "002"})
                self.assertEqual(page.context["rows"][0]["cells"][0]["score_band"], band)

    def test_empty_filter_preserves_whole_class_statistics_and_reset(self):
        result = self.client.get("/dashboard/", {"class": self.x.pk, "q": "不存在的学生"})
        self.assertEqual(result.context["page"].paginator.count, 0)
        self.assertEqual(result.context["student_count"], 3)
        self.assertContains(result, "8/20 · 40%")
        self.assertContains(result, "计入 2 人 / 全班 3 人")
        self.assertContains(result, f'href="?class={self.x.pk}"')

    def test_student_return_retains_page_and_filters(self):
        for i in range(25):
            student = get_user_model().objects.create_user(f"1{i:02d}")
            Enrollment.objects.create(teaching_class=self.x, student=student)
        data = {"class": self.x.pk, "assignment": self.tasks[1].pk, "status": "missing", "page": 2}
        report = self.client.get("/dashboard/", data)
        student = report.context["page"][0]["student"]
        detail = self.client.get(f"/teacher/students/{student.pk}/", data)
        self.assertContains(detail, f'#student-{student.pk}')
        self.assertEqual(detail.context["back_query"], report.context["back_query"])
        self.assertIn("page=2", detail.context["back_query"])

    def test_pagination_and_individual_home(self):
        for i in range(25):
            student = get_user_model().objects.create_user(f"1{i:02d}")
            Enrollment.objects.create(teaching_class=self.x,student=student)
        result=self.client.get("/dashboard/",{"class":self.x.pk,"page":2})
        self.assertEqual(len(result.context["page"]),3)
        self.assertEqual(result.context["page"].paginator.count,28)
        for name, value in [("001","16 / 20"),("002","0 / 20")]:
            self.client.force_login(self.people[name])
            result=self.client.get("/me/")
            self.assertContains(result,value)
            self.assertNotContains(result,"未发布草稿评语")
            self.assertNotContains(result,"班级成绩册")
            self.assertContains(result,"V2" if name=="001" else "V1")
        self.assertNotContains(self.client.get("/me/"),"16 / 20")
