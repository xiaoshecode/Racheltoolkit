import hashlib
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image
from pypdf import PdfWriter

from school.models import Course, Enrollment, TeachingAssignment, TeachingClass, Term
from storage import services as objects
from storage.backends import FaultBackend
from .models import Assignment, Submission
from .services import reconcile
from .validation import LIMIT

def photo(color):
    buffer = BytesIO()
    Image.new("RGB",(6,8),color).save(buffer,format="PNG")
    return buffer.getvalue()

def pdf():
    buf=BytesIO()
    writer=PdfWriter()
    writer.add_blank_page(100,100)
    writer.add_blank_page(120,150)
    writer.write(buf)
    return buf.getvalue()

class WorkflowBase(TransactionTestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory(dir=settings.RUNTIME)
        self.addCleanup(private.cleanup)
        self.folder = Path(private.name)
        override = override_settings(LOCAL_STORAGE_ROOT=self.folder)
        override.enable()
        self.addCleanup(override.disable)
        self.course=Course.objects.create(code="MATH",title="数学")
        term=Term.objects.create(code="TERM")
        self.x=TeachingClass.objects.create(course=self.course,term=term,code="X")
        self.y=TeachingClass.objects.create(course=self.course,term=term,code="Y")
        self.a,self.b,self.c,self.teacher,self.k=[get_user_model().objects.create(username=n,first_name="演示") for n in ("001","002","003","J","K")]
        for user,group in [(self.a,self.x),(self.b,self.x),(self.c,self.y)]:
            Enrollment.objects.create(student=user,teaching_class=group)
        TeachingAssignment.objects.create(teacher=self.teacher,teaching_class=self.x)
        TeachingAssignment.objects.create(teacher=self.k,teaching_class=self.y)
        now=timezone.now()
        self.work=Assignment.objects.create(teaching_class=self.x,title="练习",published_at=now-timedelta(days=1),due_at=now+timedelta(hours=1))
        self.url=f"/assignments/{self.work.pk}/submissions/"
        self.client.force_login(self.a)
    def send(self,key,*items,client=None,url=None,**extra):
        files=[SimpleUploadedFile(name,data) for name,data in items]
        return (client or self.client).post(url or self.url,{"files":files,"key":key,**extra})

class SubmissionTests(WorkflowBase):
    def test_receipt_order_replay_conflict_pdf_resubmission_and_visibility(self):
        red=("a.png",photo("red")); blue=("b.png",photo("blue"))
        first=self.send("one",blue,red)
        self.assertEqual(first.status_code,201)
        receipt=first.json()
        self.assertEqual([p["sha256"] for p in receipt["attachments"]],[hashlib.sha256(b).hexdigest() for _,b in (blue,red)])
        self.assertEqual(self.send("one",blue,red).json(),receipt)
        self.assertEqual(self.send("one",red,blue).status_code,409)
        second=self.send("two",("work.pdf",pdf()))
        self.assertEqual(second.json()["version"],2)
        self.assertEqual(second.status_code,201)
        urls=[receipt["url"],receipt["attachments"][0]["url"],f"/submissions/{receipt['id']}/download/"]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code,200)
        for u in (self.b,self.c,self.k):
            self.client.force_login(u)
            for url in urls:
                response=self.client.get(url)
                self.assertEqual(response.status_code,404)
                self.assertIn("no-store",response["Cache-Control"])
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(urls[0]).status_code,200)
        Enrollment.objects.filter(student=self.a).update(active=False)
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(urls[1]).status_code,404)

    def test_transfer_failures_preserve_old_version_and_recover_only_missing(self):
        items=[("a.png",photo("red")),("b.png",photo("green"))]
        self.assertEqual(self.send("initial",items[0]).status_code,201)
        backend=FaultBackend(self.folder,None)
        real_put=backend.put
        writes=[]
        def fail_second(path,data):
            writes.append(path)
            if len(writes)==2:
                backend.failure="after_write"
            real_put(path,data)
        with patch("storage.services.get_backend",return_value=backend),patch.object(backend,"put",side_effect=fail_second):
            pending=self.send("interrupted",*items)
            self.assertEqual(pending.status_code,202)
            self.assertNotIn("receipt",pending.json())
            self.assertEqual(Submission.objects.filter(state="confirmed").count(),1)
            sub=Submission.objects.get(pk=pending.json()["id"])
            self.assertEqual(reconcile(sub,repair=True),"confirmed")
            retry=self.send("interrupted",*items)
            self.assertEqual(retry.status_code,200)
            self.assertEqual(len(retry.json()["attachments"]),2)
            self.assertEqual(len(writes),2)
        backend=FaultBackend(self.folder,"before_write")
        with patch("storage.services.get_backend",return_value=backend):
            self.assertEqual(self.send("before",items[0]).status_code,202)
            self.assertEqual(self.send("before",items[0]).status_code,201)
        self.assertEqual(self.send("before",items[0]).status_code,200)

    def test_conflict_quarantines_without_receipt(self):
        backend=FaultBackend(self.folder,"after_write")
        with patch("storage.services.get_backend",return_value=backend):
            pending=self.send("corrupt",("a.png",photo("black")))
            sub=Submission.objects.get(pk=pending.json()["id"])
            backend.filename(sub.attachments.get().file.path).write_bytes(b"broken")
            self.assertEqual(reconcile(sub,repair=True),"quarantined")
            self.assertEqual(self.send("corrupt",("a.png",photo("black"))).status_code,409)
            self.assertFalse(Submission.objects.filter(state="confirmed").exists())

    def test_deadline_boundary_and_admitted_retry(self):
        file=("work.png",photo("white"))
        limit=self.work.due_at
        with patch("django.utils.timezone.now",return_value=limit-timedelta(microseconds=1)):
            with patch("storage.services.get_backend",return_value=FaultBackend(self.folder,"before_write")):
                self.assertEqual(self.send("admitted",file).status_code,202)
        with patch("django.utils.timezone.now",return_value=limit):
            self.assertEqual(self.send("equal",file).status_code,409)
        with patch("django.utils.timezone.now",return_value=limit+timedelta(seconds=2)):
            self.assertEqual(self.send("late",file).status_code,409)
            finished=self.send("admitted",file)
            self.assertEqual(finished.status_code,201)
            self.assertGreater(Submission.objects.get().received_at,limit)

    def test_real_file_limits_and_invalid_inputs(self):
        p=photo("yellow")
        self.assertEqual(self.send("ten",*[(f"{i}.png",p) for i in range(10)]).status_code,201)
        for key, files in [("eleven",[(f"{i}.png",p) for i in range(11)]),("mix",[("a.png",p),("b.pdf",pdf())]),
                          ("empty",[("a.doc",b"")]),("fake",[("a.jpg",b"%PDF-bad")]),("wrongext",[("a.jpg",p)])]:
            self.assertEqual(self.send(key,*files).status_code,400)
        self.assertEqual(self.send("limit",("ok.png",p+bytes(LIMIT-len(p)))).status_code,201)
        self.assertEqual(self.send("over",("big.png",p+bytes(LIMIT-len(p)+1))).status_code,413)
        legacy=self.client.post(self.url,{"key":"legacy","file":SimpleUploadedFile("legacy.png",p)})
        self.assertEqual(legacy.status_code,201)
        self.assertEqual(self.client.get(f"/submissions/{legacy.json()['id']}/download/").status_code,200)

    def test_proxy_owner_actor_note_and_roster(self):
        url=f"/teacher/assignments/{self.work.pk}/students/{self.a.pk}/submissions/"
        file=("w.png",photo("cyan"))
        self.client.force_login(self.teacher)
        data=self.send("proxy",file,url=url,note="课堂补交").json()
        self.assertEqual((data["student"],data["submitted_by"],data["version"],data["note"]),("001","J",1,"课堂补交"))
        self.assertEqual(self.send("proxy",file,url=url,note="课堂补交").status_code,200)
        self.assertEqual(self.send("forge",file,url=url,note="test",source="student").status_code,400)
        self.assertContains(self.client.get(f"/assignments/{self.work.pk}/"),"未交")
        self.client.force_login(self.k)
        self.assertEqual(self.send("bad",file,url=url,note="理由").status_code,404)
        self.client.force_login(self.a)
        self.assertEqual(self.send("proxy",file).status_code,409)
        self.assertEqual(self.send("own",file).json()["version"],2)

    def test_reverse_finish_and_same_key_inflight(self):
        entered=threading.Event(); release=threading.Event()
        client=Client(); client.force_login(self.a)
        file=("w.png",photo("lime"))
        original=objects.ensure_written
        def delayed(record,content):
            if threading.current_thread().name.startswith("first"):
                entered.set()
                if not release.wait(15): raise RuntimeError("test writer timed out")
            return original(record,content)
        def first():
            try:
                return self.send("first",file,client=client)
            finally:
                connections.close_all()
        with patch("storage.services.ensure_written",side_effect=delayed),ThreadPoolExecutor(max_workers=1,thread_name_prefix="first") as pool:
            future=pool.submit(first)
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(self.send("first",file).status_code,202)
                self.assertEqual(self.send("second",file).json()["version"],2)
            finally:
                release.set()
            self.assertEqual(future.result().status_code,201)
        self.assertEqual(list(Submission.objects.filter(state="confirmed").order_by("-version").values_list("version",flat=True)),[2,1])

