import hashlib
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfWriter

from school.models import Course, CourseEditor, Enrollment, TeachingAssignment, TeachingClass, Term
from storage.backends import LocalBackend, StorageUnavailable
from storage.previews import prepare_preview
from submissions.models import Assignment
from .models import CoursePublication
from .services import draft_for, published_chapter_exists


def pdf():
    out = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=420)
    writer.write(out)
    return out.getvalue()


def slides(content):
    out = BytesIO()
    with ZipFile(out, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("ppt/presentation.xml", content)
    return out.getvalue()


class CoursePublicationTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=settings.RUNTIME)
        self.addCleanup(temp.cleanup)
        storage = patch("storage.services.get_backend", return_value=LocalBackend(Path(temp.name)))
        storage.start()
        self.addCleanup(storage.stop)
        self.course = Course.objects.create(code="C1", title="信息素养")
        self.teacher, self.a, self.b, self.c = [
            get_user_model().objects.create_user(name) for name in ["J", "001", "002", "003"]
        ]
        term = Term.objects.create(code="F26")
        self.x, self.y = [TeachingClass.objects.create(term=term, course=self.course, code=n) for n in ["X", "Y"]]
        TeachingAssignment.objects.create(teaching_class=self.x, teacher=self.teacher)
        CourseEditor.objects.create(course=self.course, teacher=self.teacher)
        for user, group in [(self.a,self.x),(self.b,self.x),(self.c,self.y)]:
            Enrollment.objects.create(teaching_class=group, student=user)
        self.client.force_login(self.teacher)
        self.editor = reverse("catalog_editor", args=[self.course.pk])
        self.course_url = reverse("catalog_course", args=[self.course.pk])

    def post(self, action, *, status=302, version=None, **fields):
        version = draft_for(self.course).edit_version if version is None else version
        response = self.client.post(self.editor, {"edit_version":version, "action":action, **fields})
        self.assertEqual(response.status_code, status)
        return response

    def test_draft_publication_conflict_and_permissions(self):
        self.post("add_section", title="第一章", body="旧版正文")
        self.post("publish")
        old = draft_for(self.course).edit_version
        self.post("add_section", title="第二章", body="新增正文")
        chapter = draft_for(self.course).sections.last()
        self.post("up", section=chapter.stable_key)
        self.post("course", title="陈旧输入", version=old, status=409)
        self.assertContains(self.client.get(reverse("catalog_preview", args=[self.course.pk])), "新增正文")
        self.assertNotContains(self.client.get(self.course_url), "新增正文")
        self.post("publish")
        revision = CoursePublication.objects.get(course=self.course).revision
        self.assertEqual(list(revision.sections.values_list("title", flat=True)), ["第二章","第一章"])
        historical = self.course.revisions.get(seq=1)
        self.assertEqual(list(historical.sections.values_list("title", flat=True)), ["第一章"])
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(self.editor).status_code,404)
        self.assertEqual(self.client.post(self.editor, {"action":"publish","edit_version":1}).status_code,404)
        self.assertEqual(self.client.get(reverse("catalog_preview",args=[self.course.pk])).status_code,404)
        self.client.logout()
        self.assertContains(self.client.get(self.course_url),"新增正文")

    def test_public_files_replaced_withdrawn_and_preview_denied(self):
        self.post("add_section", title="讲义")
        sec = draft_for(self.course).sections.get().stable_key
        p1, p2, preview = slides("<p>R1</p>"), slides("<p>R2</p>"), pdf()
        def attach(data, key=""):
            self.post("upload", section=sec, material=key, title="课件",
                      original=SimpleUploadedFile("example.pptx",data),
                      preview=SimpleUploadedFile("reading.pdf",preview))
            return draft_for(self.course).sections.get().materials.get()
        def link(item,mode):
            file = item.original_file if mode == "download" else item.preview_file
            return reverse("catalog_file",args=[self.course.pk,item.stable_key,file.pk,mode])
        initial = attach(p1)
        download, reading = link(initial,"download"), link(initial,"preview")
        self.assertEqual(self.client.get(download).status_code,404)
        self.post("publish")
        newer = attach(p2,initial.stable_key)
        self.assertEqual(self.client.get(link(newer,"download")).status_code,404)
        self.assertEqual(b"".join(self.client.get(download).streaming_content),p1)
        self.post("publish")
        for identity in (None, self.a, self.b):
            if identity:
                self.client.force_login(identity)
            else:
                self.client.logout()
            for kind, data in [("download",p2),("preview",preview)]:
                response = self.client.get(link(newer,kind))
                self.assertEqual(response.status_code,200)
                self.assertEqual(hashlib.sha256(b"".join(response.streaming_content)).digest(),
                                 hashlib.sha256(data).digest())
        self.assertEqual(self.client.get(download).status_code,404)
        self.assertEqual(self.client.get(reading).status_code,404)
        self.client.force_login(self.teacher)
        self.post("remove", section=sec, material=newer.stable_key)
        self.post("publish")
        for kind in ["preview","download"]:
            self.assertEqual(self.client.get(link(newer,kind)).status_code,404)

    def test_unready_preview_or_storage_failure_preserves_publication(self):
        self.post("add_section", title="未改版章节")
        self.post("publish")
        sec = draft_for(self.course).sections.get().stable_key
        with patch("catalog.views.receive_file", side_effect=StorageUnavailable):
            self.post("upload", section=sec, status=503, original=SimpleUploadedFile("f.pdf",pdf()))
        self.assertFalse(draft_for(self.course).sections.get().materials.exists())
        self.post("upload",section=sec,original=SimpleUploadedFile("f.pptx",slides("<p/>")))
        self.post("publish",status=400)
        self.assertEqual(CoursePublication.objects.get(course=self.course).revision.seq,1)
        self.assertNotContains(self.client.get(self.course_url),"f.pptx")

    def test_generated_course_preview_is_available_to_editor_before_publication(self):
        self.post("add_section", title="自动预览")
        section = draft_for(self.course).sections.get().stable_key
        self.post("upload", section=section, original=SimpleUploadedFile("slides.pptx", slides("<p/>")))
        item = draft_for(self.course).sections.get().materials.get()
        with patch("storage.previews.convert_office", return_value=pdf()):
            asset = prepare_preview(item.original_file)
        self.assertEqual(asset.state, "ready")
        draft_url = reverse("catalog_draft_file", args=[self.course.pk, item.stable_key,
                                                       asset.output_file_id, "preview"])
        self.assertContains(self.client.get(self.editor), "预览已就绪")
        preview = self.client.get(reverse("catalog_preview", args=[self.course.pk]))
        self.assertContains(preview, draft_url + "?reader=1")
        reader = self.client.get(draft_url, {"reader": "1"})
        self.assertContains(reader, 'id="course-reader"')
        self.assertContains(reader, f'data-source="{draft_url}"')
        self.assertIn("no-store", reader["Cache-Control"])
        self.assertEqual(b"".join(self.client.get(draft_url).streaming_content), pdf())
        item.refresh_from_db()
        self.assertIsNone(item.preview_file_id)
        self.assertFalse(CoursePublication.objects.filter(course=self.course).exists())
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(draft_url).status_code, 404)
        self.assertEqual(self.client.get(draft_url, {"reader": "1"}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(draft_url).status_code, 404)
        self.assertEqual(self.client.get(draft_url, {"reader": "1"}).status_code, 404)
        self.client.force_login(self.teacher)
        self.post("publish")
        public_url = reverse("catalog_file", args=[self.course.pk, item.stable_key,
                                                   asset.output_file_id, "preview"])
        self.assertEqual(b"".join(self.client.get(public_url).streaming_content), pdf())

    def test_public_pdf_reader_and_bytes_share_publication_and_withdrawal_rules(self):
        self.post("add_section", title="阅读讲义")
        section = draft_for(self.course).sections.get().stable_key
        self.post("upload", section=section, title='<script>alert("title")</script>',
                  original=SimpleUploadedFile("notes.pdf", pdf()))
        item = draft_for(self.course).sections.get().materials.get()
        url = reverse("catalog_file", args=[self.course.pk, item.stable_key, item.preview_file_id, "preview"])
        download = reverse("catalog_file", args=[self.course.pk, item.stable_key, item.original_file_id, "download"])
        self.assertEqual(self.client.get(url, {"reader": "1"}).status_code, 404)
        self.post("publish")
        self.client.logout()
        response = self.client.get(url, {"reader": "1"})
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertContains(response, 'id="course-reader"')
        self.assertContains(response, f'data-source="{url}"')
        self.assertContains(response, download)
        self.assertContains(response, "/static/catalog/reader.js")
        self.assertNotContains(response, '<script>alert("title")</script>')
        self.assertContains(self.client.get(self.course_url), url + "?reader=1")
        self.assertEqual(b"".join(self.client.get(url).streaming_content), pdf())
        self.client.force_login(self.teacher)
        self.post("remove", section=section, material=item.stable_key)
        self.post("publish")
        self.client.logout()
        self.assertEqual(self.client.get(url, {"reader": "1"}).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(download).status_code, 404)

    def test_course_preview_failure_can_retry_without_changing_published_course(self):
        self.post("add_section", title="已发布内容")
        self.post("publish")
        section = draft_for(self.course).sections.get().stable_key
        self.post("upload", section=section, original=SimpleUploadedFile("slides.pptx", slides("<p/>")))
        item = draft_for(self.course).sections.get().materials.get()
        with patch("storage.previews.convert_office", side_effect=RuntimeError("conversion failed")):
            asset = prepare_preview(item.original_file)
        self.assertEqual(asset.state, "failed")
        self.assertContains(self.client.get(self.editor), "预览生成失败")
        previous = draft_for(self.course).edit_version
        self.post("retry_preview", section=section, material=item.stable_key)
        asset.refresh_from_db()
        self.assertEqual(asset.state, "pending")
        self.assertEqual(CoursePublication.objects.get(course=self.course).revision.seq, 1)
        self.assertNotContains(self.client.get(self.course_url), "slides.pptx")
        self.post("retry_preview", section=section, material=item.stable_key, version=previous, status=409)
        self.client.force_login(self.a)
        self.post("retry_preview", section=section, material=item.stable_key, status=404)

    def test_t12_stable_chapter_and_scoped_assignment(self):
        self.post("add_section", title="共享章")
        chapter = draft_for(self.course).sections.get().stable_key
        self.assertFalse(published_chapter_exists(self.course.pk,chapter))
        self.post("publish")
        self.assertTrue(published_chapter_exists(self.course.pk,chapter))
        link = reverse("catalog_chapter", args=[self.course.pk,chapter])
        now = timezone.now()
        for group, title in [(self.x,"X班专属练习"),(self.y,"Y班专属练习")]:
            Assignment.objects.create(teaching_class=group,title=title,chapter_key=chapter,
                                      published_at=now,due_at=now+timedelta(days=1))
        self.post("section",section=chapter,title="更新后的章",body="打开固定链接")
        self.post("publish")
        for identity,yes,no in [(self.a,"X班专属练习","Y班专属练习"),(self.b,"X班专属练习","Y班专属练习"),
                                (self.c,"Y班专属练习","X班专属练习")]:
            self.client.force_login(identity)
            r = self.client.get(link)
            self.assertContains(r,"更新后的章")
            self.assertContains(r,yes)
            self.assertNotContains(r,no)
            api = self.client.get(reverse("catalog_api"))
            self.assertNotIn("Access-Control-Allow-Origin",api)
            self.assertNotContains(api,"专属练习")
            self.assertEqual(api.json()["courses"][0]["chapters"][0]["url"],link)
        self.client.logout()
        self.assertNotContains(self.client.get(link),"专属练习")
        self.assertContains(self.client.get(link),"更新后的章")

    def test_public_api_cors_allows_only_the_pages_origin(self):
        self.client.logout()
        url = reverse("catalog_api")
        allowed = settings.PUBLIC_PORTAL_ORIGIN
        for origin in [allowed, None, "null", allowed + ".example.org", "https://other.github.io"]:
            headers = {"HTTP_ORIGIN": origin} if origin is not None else {}
            response = self.client.get(url, **headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get("Access-Control-Allow-Origin"), allowed if origin == allowed else None)
            self.assertNotIn("Access-Control-Allow-Credentials", response)
            self.assertIn("Origin", response["Vary"])

    def test_chapter_navigation_tracks_publication_scope_and_saved_chapter(self):
        self.post("add_section", title="已公布第一章")
        first = draft_for(self.course).sections.get()
        self.post("add_section", title="已公布第二章")
        second = draft_for(self.course).sections.last()
        self.post("publish")
        self.post("add_section", title="教师未公布章节")
        saved = self.post("section", section=first.stable_key, title="待发布改名", body="正文")
        self.assertEqual(saved["Location"], self.editor + f"#chapter-{first.stable_key}")
        preview = self.client.get(reverse("catalog_preview", args=[self.course.pk]))
        self.assertContains(preview, "教师未公布章节")
        self.client.logout()
        chapter_url = reverse("catalog_chapter", args=[self.course.pk, first.stable_key])
        response = self.client.get(chapter_url)
        self.assertEqual(response.context["current_chapter"].stable_key, first.stable_key)
        self.assertEqual([chapter.stable_key for chapter in response.context["outline"]],
                         [first.stable_key, second.stable_key])
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, reverse("catalog_chapter", args=[self.course.pk, second.stable_key]))
        self.assertNotContains(response, "教师未公布章节")
        self.assertNotContains(response, "待发布改名")
        filtered = self.client.get(self.course_url, {"q": "第一章"})
        self.assertEqual([chapter.stable_key for chapter in filtered.context["outline"]], [first.stable_key])
        self.assertNotContains(filtered, "已公布第二章")

    def test_search_uses_published_titles_keywords_file_names_and_format(self):
        self.assertNotContains(self.client.get(self.editor),"查看已发布课程")
        self.post("add_section", title="证据来源", body="比较可信度")
        sec = draft_for(self.course).sections.get().stable_key
        self.post("upload",section=sec,title="方法讲义",original=SimpleUploadedFile("Notes.PDF",pdf()))
        self.post("add_section", title="数据分析")
        self.post("publish")
        self.assertContains(self.client.get(self.editor),"查看已发布课程")
        self.post("add_section",title="未公布的题库")
        self.client.logout()
        chapter_url = reverse("catalog_chapter",args=[self.course.pk,sec])
        for query in ["证据来源","可信度","方法讲义","notes.pdf"]:
            criteria = {"q": query, "format": "pdf"}
            self.assertContains(self.client.get("/",criteria),"证据来源")
            for route in [self.course_url,chapter_url]:
                result=self.client.get(route,criteria)
                self.assertContains(result,"Notes.PDF")
                self.assertNotContains(result,"数据分析")
                self.assertContains(result,"下载 PDF")
            items=self.client.get(reverse("catalog_api"),criteria).json()["courses"]
            self.assertEqual(items[0]["chapters"][0]["materials"][0], {
                "title":"方法讲义","format":"PDF","size":len(pdf())})
        for criteria in [{"q":"题库"},{"q":"方法讲义","format":"PPTX"}]:
            self.assertEqual(self.client.get(reverse("catalog_api"),criteria).json()["courses"],[])
            self.assertContains(self.client.get(self.course_url,criteria),"没有符合条件")
