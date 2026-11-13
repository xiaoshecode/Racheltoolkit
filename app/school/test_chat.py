import http.client
from io import BytesIO
import json
import sqlite3
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings

from .chat import ChatError, Lease, MAX_BODY, parse_messages, route_model
from .models import Course, CourseEditor, Enrollment, TeachingClass, Term

URL = "/api/assistant/chat/"
QUESTION = {"role": "user", "content": "怎样理解一次函数的斜率？"}


def upstream(*chunks, done=True, status=200):
    data = b": keep-alive\r\n\r\n"
    for chunk in chunks:
        data += ("data: " + json.dumps(chunk, ensure_ascii=False) + "\r\n\r\n").encode()
    if done:
        data += b"data: [DONE]\r\n\r\n"
    response = BytesIO(data)
    response.status = status
    response.getheader = lambda key, default="": "text/event-stream; charset=utf-8"
    return response


def delta(content=None, reasoning=None, finish=None):
    fields = {}
    if content is not None:
        fields["content"] = content
    if reasoning is not None:
        fields["reasoning_content"] = reasoning
    return {"choices": [{"delta": fields, "finish_reason": finish}]}


class AssistantTests(TransactionTestCase):
    def setUp(self):
        term = Term.objects.create(code="AI-test")
        course = Course.objects.create(code="AI-Math", title="数学")
        self.group = TeachingClass.objects.create(term=term, course=course, code="AI-X")
        self.student = get_user_model().objects.create_user("ai-student")
        self.membership = Enrollment.objects.create(student=self.student, teaching_class=self.group)
        self.outsider = get_user_model().objects.create_user("ai-outsider")
        self.editor = get_user_model().objects.create_user("ai-editor")
        CourseEditor.objects.create(course=course, teacher=self.editor)

        temp = TemporaryDirectory(dir=settings.RUNTIME)
        self.addCleanup(temp.cleanup)
        override = override_settings(RUNTIME=Path(temp.name), DEEPSEEK_API_KEY="synthetic-ai-key",
            DEEPSEEK_MODEL="auto", DEEPSEEK_TIMEOUT_SECONDS=60, DEEPSEEK_MAX_TOKENS=4096)
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(self.student)

    def send(self, **changes):
        payload = {"messages": [QUESTION], "thinking": False, "consent": True} | changes
        return self.client.post(URL, json.dumps(payload), content_type="application/json")

    def provider(self, response):
        connection = MagicMock()
        connection.getresponse.return_value = response
        patcher = patch("school.chat.http.client.HTTPSConnection", return_value=connection)
        constructor = patcher.start()
        self.addCleanup(patcher.stop)
        timer_patcher = patch("school.chat.threading.Timer")
        timer = timer_patcher.start()
        self.addCleanup(timer_patcher.stop)
        return connection, constructor, timer

    def events(self, response):
        body = b"".join(response.streaming_content).decode()
        response.close()
        return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]

    def test_login_membership_revocation_and_csrf_enforced(self):
        self.client.logout()
        self.assertRedirects(self.client.get("/assistant/"), "/login/?next=/assistant/")
        self.assertEqual(self.send().status_code, 401)
        self.client.force_login(self.outsider)
        self.assertEqual(self.send().status_code, 403)
        self.assertEqual(self.client.get("/assistant/").status_code, 403)
        self.assertNotContains(self.client.get("/me/"), 'href="/assistant/"')
        self.client.force_login(self.student)
        response = self.client.get("/assistant/")
        self.assertContains(response, 'href="/assistant/" aria-current="page"')
        self.assertNotContains(response, settings.DEEPSEEK_API_KEY)
        Enrollment.objects.filter(pk=self.membership.pk).update(active=False)
        self.assertEqual(self.send().status_code, 403)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get("/assistant/").status_code, 200)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.editor)
        self.assertEqual(csrf.post(URL, "{}", content_type="application/json").status_code, 403)
        self.assertEqual(self.client.get(URL).status_code, 405)

    def test_auto_policy_is_available_to_the_assistant_page(self):
        response = self.client.get("/assistant/")
        self.assertTrue(response.context["assistant_enabled"])
        self.assertEqual(response.context["assistant_model"], "auto")

    def test_manual_model_overrides_auto_signals_and_is_announced(self):
        for selected, thinking in (("deepseek-flash", True), ("deepseek-v4-pro", False)):
            with self.subTest(selected=selected):
                connection = MagicMock()
                connection.getresponse.return_value = upstream(delta(content="回复", finish="stop"))
                with override_settings(DEEPSEEK_MODEL=selected), \
                        patch("school.chat.http.client.HTTPSConnection", return_value=connection), \
                        patch("school.chat.threading.Timer"):
                    events = self.events(self.send(thinking=thinking))
                payload = json.loads(connection.request.call_args.kwargs["body"])
                self.assertEqual(payload["model"], selected)
                self.assertEqual(payload["thinking"], {"type": "enabled" if thinking else "disabled"})
                self.assertEqual(events[0], {"type": "model", "model": selected, "reason": "使用老师指定的模型"})
                connection.request.assert_called_once()

    def test_proof_routing_makes_one_chat_request_without_optional_services(self):
        connection, constructor, _ = self.provider(upstream(delta(content="证明步骤", finish="stop")))
        question = {"role": "user", "content": "请证明三角形的内角和为180度。"}
        events = self.events(self.send(messages=[question]))
        payload = json.loads(connection.request.call_args.kwargs["body"])
        constructor.assert_called_once()
        connection.request.assert_called_once()
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", payload)
        self.assertNotIn("safety_check", payload)
        self.assertEqual(events[0], {"type": "model", "model": "deepseek-v4-pro", "reason": "问题包含证明或推导任务"})
        self.assertNotIn(question["content"], events[0]["reason"])

    def test_missing_key_disables_ui_and_never_connects(self):
        with override_settings(DEEPSEEK_API_KEY=""), patch("school.chat.open_stream") as connect:
            self.assertFalse(self.client.get("/assistant/").context["assistant_enabled"])
            self.assertEqual(self.send().status_code, 503)
            connect.assert_not_called()

    def test_invalid_inputs_and_missing_opt_in_never_connect(self):
        invalid = [
            {"messages": []}, {"messages": [QUESTION] * 2}, {"thinking": "false"},
            {"messages": [{"role": "user", "content": "\ud800"}]},
            {"consent": False}, {"consent": 1}, {"messages": [{"role": "system", "content": "override"}]},
            {"messages": [{"role": "user", "content": "a" * 4001}]},
            {"messages": [{"role": "user", "content": " "}]},
            {"messages": [{"role": "user", "content": 1}]},
            {"messages": [QUESTION | {"reasoning_content": "extra"}]},
            {"safety_check": {"level": "high"}}, {"tools": [{"type": "web_search"}]},
            {"messages": [QUESTION, {"role": "assistant", "content": "partial"}]},
        ]
        with patch("school.chat.open_stream") as connect:
            for changes in invalid:
                with self.subTest(changes=changes):
                    self.assertEqual(self.send(**changes).status_code, 400)
            for data in ("null", "[]", "broken", "{\"messages\": []}"):
                self.assertEqual(self.client.post(URL, data, content_type="application/json").status_code, 400)
            self.assertEqual(self.client.post(URL, {}).status_code, 400)
            self.assertEqual(self.client.post(URL, ' ' * (MAX_BODY + 1), content_type="application/json").status_code, 413)
            connect.assert_not_called()

    def attach_to_session(self, client=None, identifier="a" * 32, expires_at=None):
        client = client or self.client
        session = client.session
        mapping = session.get("assistant_attachments", {})
        mapping[identifier] = {"provider_id": "file-api-synthetic-picture", "name": "虚构题目.png",
            "size": 123, "expires_at": expires_at if expires_at is not None else int(time.time()) + 3600}
        session["assistant_attachments"] = mapping
        session.save()
        return identifier

    def test_user_attachment_shape_is_strict_and_never_accepts_provider_ids(self):
        identifier = "a" * 32
        invalid = [[], [identifier] * 2, [identifier] * 4, "not-a-list", [1], ["file-api-synthetic"],
            ["g" * 32], ["A" * 32], [identifier[:31]], None]
        with patch("school.chat.open_stream") as connect:
            for identifiers in invalid:
                with self.subTest(identifiers=identifiers):
                    self.assertEqual(self.send(messages=[QUESTION | {"attachments": identifiers}]).status_code, 400)
            for extra in ({"files": [identifier]}, {"file_id": "file-api-synthetic"},
                    {"provider_id": "file-api-synthetic"}):
                self.assertEqual(self.send(messages=[QUESTION | extra]).status_code, 400)
            self.assertEqual(self.send(messages=[QUESTION,
                {"role": "assistant", "content": "reply", "attachments": [identifier]}, QUESTION]).status_code, 400)
            self.assertEqual(self.send(messages=[QUESTION | {"content": "", "attachments": [identifier]}]).status_code, 400)
            connect.assert_not_called()

    def test_attachment_references_are_bound_to_current_login_session(self):
        identifier = self.attach_to_session()
        other_session = Client()
        other_session.force_login(self.student)
        payload = {"messages": [QUESTION | {"attachments": [identifier]}], "consent": True}
        with patch("school.chat.open_stream") as connect, patch("school.chat.Lease") as lease:
            response = other_session.post(URL, json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("file-api-", response.content.decode())
            connect.assert_not_called()
            lease.assert_not_called()

    def test_expired_attachment_fails_before_spending_and_requires_opt_in(self):
        identifier = self.attach_to_session(expires_at=int(time.time()) - 1)
        with patch("school.chat.open_stream") as connect, patch("school.chat.Lease") as lease:
            response = self.send(messages=[QUESTION | {"attachments": [identifier]}])
            self.assertEqual(response.status_code, 400)
            self.assertIn("过期", response.json()["error"])
            self.assertEqual(self.send(messages=[QUESTION | {"attachments": [identifier]}], consent=False).status_code, 400)
            connect.assert_not_called()
            lease.assert_not_called()

    def test_attachment_analysis_uses_flash_and_only_resolved_file_content(self):
        identifier = self.attach_to_session()
        connection, _, _ = self.provider(upstream(delta(reasoning="理解题图"), delta(content="解题步骤", finish="stop")))
        with override_settings(DEEPSEEK_MODEL="deepseek-v4-pro"):
            events = self.events(self.send(messages=[QUESTION | {"attachments": [identifier]}], thinking=True))
        payload = json.loads(connection.request.call_args.kwargs["body"])
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": [
            {"type": "file", "file_id": "file-api-synthetic-picture"},
            {"type": "text", "text": QUESTION["content"]}]})
        self.assertEqual(events[0], {"type": "model", "model": "deepseek-flash", "reason": "附件分析使用视觉模型"})
        body = connection.request.call_args.kwargs["body"].decode()
        for private in (identifier, "虚构题目.png", "attachments", "expires_at", "provider_id"):
            self.assertNotIn(private, body)
        self.assertNotIn("tools", payload)
        self.assertNotIn("safety_check", payload)

    def test_followup_reuses_session_file_without_reuploading(self):
        identifier = self.attach_to_session()
        initial = QUESTION | {"attachments": [identifier]}
        with patch("school.chat_uploads.upload_to_provider") as upload:
            for messages in ([initial], [initial, {"role": "assistant", "content": "先画出函数图像。"},
                    {"role": "user", "content": "请证明后续结论。"}]):
                connection = MagicMock()
                connection.getresponse.return_value = upstream(delta(content="继续说明", finish="stop"))
                with patch("school.chat.http.client.HTTPSConnection", return_value=connection), \
                        patch("school.chat.threading.Timer"):
                    events = self.events(self.send(messages=messages))
                payload = json.loads(connection.request.call_args.kwargs["body"])
                self.assertEqual(payload["model"], "deepseek-flash")
                self.assertEqual(payload["messages"][1]["content"][0],
                    {"type": "file", "file_id": "file-api-synthetic-picture"})
                self.assertEqual(events[0]["reason"], "附件分析使用视觉模型")
                connection.request.assert_called_once()
                if len(messages) > 1:
                    self.assertEqual(payload["messages"][2:], messages[1:])
            upload.assert_not_called()

    def test_upload_duration_holds_same_shared_slot_until_upload_deadline(self):
        with patch("school.chat.time.time", return_value=1000), \
                patch("school.chat.time.monotonic", return_value=100):
            first = Lease(self.student.pk, duration=300)
        self.assertEqual(first.deadline, 400)
        with sqlite3.connect(first.database) as connection:
            self.assertEqual(connection.execute("SELECT lease_until FROM requests WHERE user_id = ?",
                (self.student.pk,)).fetchone()[0], 1315)
        with patch("school.chat.time.time", return_value=1200), self.assertRaises(ChatError):
            Lease(self.student.pk)
        first.close()

    def test_history_budget_rejected(self):
        conversation = []
        for index in range(6):
            conversation.extend([{"role": "user", "content": "a" * 3000},
                {"role": "assistant", "content": "b" * 3000}])
        with self.assertRaises(ChatError):
            parse_messages({"messages": [*conversation, QUESTION], "consent": True})

    def test_stream_separates_thinking_and_text_and_sends_only_explicit_history(self):
        provider = upstream(delta(reasoning="先理解斜率。"), delta(content="斜率表示"),
            delta(content="变化率。", finish="stop"))
        connection, constructor, timer = self.provider(provider)
        history = [QUESTION, {"role": "assistant", "content": "变化率。"},
            {"role": "user", "content": "能给出一个例子吗？"}]
        response = self.send(messages=history, thinking=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertIn("no-store", response["Cache-Control"])
        events = self.events(response)
        self.assertEqual([entry["type"] for entry in events], ["model", "reasoning", "content", "content", "done"])
        self.assertEqual(events[0], {"type": "model", "model": "deepseek-v4-pro", "reason": "深度思考已开启"})
        self.assertEqual(events[-1]["finish_reason"], "stop")
        constructor.assert_called_once_with("api.deepseek.com", timeout=60)
        args = connection.request.call_args
        self.assertEqual(args.args, ("POST", "/chat/completions"))
        payload = json.loads(args.kwargs["body"])
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        connection.request.assert_called_once()
        self.assertEqual(payload["messages"][1:], history)
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertNotIn("tools", payload)
        self.assertNotIn("safety_check", payload)
        self.assertNotIn("consent", payload)
        self.assertNotIn("ai-student", args.kwargs["body"].decode())
        self.assertEqual(args.kwargs["headers"]["Authorization"], "Bearer synthetic-ai-key")
        self.assertTrue(provider.closed)
        timer.return_value.cancel.assert_called()
        next_lease = Lease(self.student.pk)
        next_lease.close()

    def test_nonthinking_mode_and_truncated_finish(self):
        connection, _, _ = self.provider(upstream(delta(content="部分说明", finish="length")))
        events = self.events(self.send())
        self.assertEqual(events[0], {"type": "model", "model": "deepseek-flash", "reason": "常规学习问题"})
        self.assertEqual(events[-1], {"type": "done", "finish_reason": "length"})
        self.assertEqual(json.loads(connection.request.call_args.kwargs["body"])["model"], "deepseek-flash")
        self.assertEqual(json.loads(connection.request.call_args.kwargs["body"])["thinking"], {"type": "disabled"})

    def test_abrupt_disconnect_is_error_and_releases_slot(self):
        provider = upstream(delta(content="未完成"), done=False)
        connection, _, _ = self.provider(provider)
        events = self.events(self.send())
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(any(entry["type"] == "done" for entry in events))
        self.assertTrue(provider.closed)
        connection.close.assert_called()
        Lease(self.student.pk).close()

    def test_malformed_sse_and_abnormal_finish_do_not_report_success(self):
        for response in (upstream({"choices": "bad"}), upstream({"choices": ["bad"]}),
                upstream(delta(content="partial", finish="content_filter")),
                upstream(delta(reasoning="thinking", finish="length"))):
            with self.subTest(response=response):
                connection = MagicMock()
                connection.getresponse.return_value = response
                with patch("school.chat.http.client.HTTPSConnection", return_value=connection), \
                        patch("school.chat.threading.Timer"):
                    events = self.events(self.send())
                self.assertEqual(events[-1]["type"], "error")
                self.assertTrue(response.closed)

    def test_http_errors_are_sanitized_and_release_slot(self):
        for status, expected in ((401, 503), (402, 503), (429, 429), (500, 502), (302, 502)):
            response = upstream(status=status)
            connection = MagicMock()
            connection.getresponse.return_value = response
            with patch("school.chat.http.client.HTTPSConnection", return_value=connection), \
                    patch("school.chat.threading.Timer"):
                result = self.send()
            self.assertEqual(result.status_code, expected)
            self.assertNotIn(settings.DEEPSEEK_API_KEY, result.content.decode())
            self.assertTrue(response.closed)
            connection.close.assert_called()

    def test_network_failure_does_not_expose_credentials(self):
        connection = MagicMock()
        connection.request.side_effect = OSError("synthetic-ai-key internal network detail")
        with patch("school.chat.http.client.HTTPSConnection", return_value=connection), \
                patch("school.chat.threading.Timer"):
            response = self.send()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("synthetic-ai-key", response.content.decode())
        Lease(self.student.pk).close()

    def test_closing_unconsumed_response_releases_resources(self):
        provider = upstream(delta(content="unused", finish="stop"))
        connection, _, timer = self.provider(provider)
        response = self.send()
        response.close()
        self.assertTrue(provider.closed)
        timer.return_value.cancel.assert_called()
        connection.close.assert_called()
        Lease(self.student.pk).close()

    def test_active_and_per_minute_limits_are_shared_and_expire(self):
        first = Lease(self.student.pk)
        with self.assertRaises(ChatError) as error:
            Lease(self.student.pk)
        self.assertEqual(error.exception.status, 429)
        first.close()
        for _ in range(7):
            Lease(self.student.pk).close()
        with self.assertRaises(ChatError):
            Lease(self.student.pk)
        with patch("school.chat.time.time", return_value=9_999_999_999):
            Lease(self.student.pk).close()
        leases = [Lease(10000 + index) for index in range(4)]
        with self.assertRaises(ChatError):
            Lease(20000)
        for lease in leases:
            lease.close()
        Lease(20000).close()

    def test_slow_connect_is_counted_inside_total_budget(self):
        connection = MagicMock()
        provider = upstream(delta(content="answer", finish="stop"))
        connection.getresponse.return_value = provider
        with patch("school.chat.http.client.HTTPSConnection", return_value=connection), \
                patch("school.chat.threading.Timer") as timer, \
                patch("school.chat.time.monotonic", side_effect=[100, 170, 170, 170, 170, 170, 170]):
            response = self.send()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(timer.call_args.args[0], 50)
        response.close()

    def test_close_failure_still_releases_user_slot(self):
        connection, _, _ = self.provider(upstream(delta(content="unused", finish="stop")))
        connection.close.side_effect = OSError("synthetic connection close failure")
        response = self.send()
        response.close()
        Lease(self.student.pk).close()


@override_settings(DEEPSEEK_MODEL="auto")
class ModelRoutingTests(SimpleTestCase):
    def route(self, question, thinking=False):
        return route_model([{"role": "user", "content": question}], thinking)

    def test_regular_analysis_and_proof_concept_questions_use_flash(self):
        for question in ("你好", "帮我分析一次函数的图像。", "解释什么是数学证明。", "证明是什么？",
                "证明题如何入手？", "证明的方法有哪些？", "请给出证明的定义。",
                "Please explain the concept of mathematical proof."):
            with self.subTest(question=question):
                self.assertEqual(self.route(question)[0], "deepseek-flash")

    def test_explicit_proof_and_derivation_requests_use_pro(self):
        for question in ("请证明三角形的内角和是180度。", "已知 n 是整数，请严格证明 n²+n 是偶数。",
                "推导二次方程的求根公式。", "Please derive the quadratic formula.", "Prove that sqrt(2) is irrational."):
            with self.subTest(question=question):
                self.assertEqual(self.route(question), ("deepseek-v4-pro", "问题包含证明或推导任务"))

    def test_thinking_routes_to_pro_without_changing_question(self):
        messages = [QUESTION.copy()]
        original = [QUESTION.copy()]
        self.assertEqual(route_model(messages, True), ("deepseek-v4-pro", "深度思考已开启"))
        self.assertEqual(messages, original)

    def test_long_question_and_context_thresholds(self):
        self.assertEqual(self.route("题" * 1199)[0], "deepseek-flash")
        self.assertEqual(self.route("题" * 1200), ("deepseek-v4-pro", "当前问题较长"))
        history = [{"role": "user", "content": "题" * 3999},
            {"role": "assistant", "content": "答" * 3998}, {"role": "user", "content": "你好"}]
        self.assertEqual(route_model(history, False)[0], "deepseek-flash")
        history[1]["content"] += "答"
        self.assertEqual(route_model(history, False), ("deepseek-v4-pro", "对话上下文较长"))

    def test_multiple_explicit_constraints_and_numbered_cases_use_pro(self):
        constraints = "已知一个排课问题，课程满足约束，且每位老师至少有一天休息。" + "详细背景说明。" * 30
        numbered = "分析以下排课问题：\n1. 第一位老师只能上午授课。\n2. 第二位老师不能周二授课。\n3. 两个班必须错开。\n" + "详细背景说明。" * 25
        for question in (constraints, numbered):
            with self.subTest(question=question):
                self.assertEqual(self.route(question), ("deepseek-v4-pro", "问题包含多项条件或约束"))
        ordinary = "帮我分析这个概念，用浅显的例子说明。" + "背景描述。" * 40
        self.assertEqual(self.route(ordinary)[0], "deepseek-flash")
        self.assertEqual(self.route("已知 x 满足条件，且 x 至少为1，求x。")[0], "deepseek-flash")

    def test_multiple_conditions_require_minimum_detail(self):
        prefix = "已知三个数满足条件，且每个数至少为1。"
        self.assertEqual(self.route(prefix + "题" * (179 - len(prefix)))[0], "deepseek-flash")
        self.assertEqual(self.route(prefix + "题" * (180 - len(prefix))),
            ("deepseek-v4-pro", "问题包含多项条件或约束"))

    def test_old_proof_turn_does_not_force_simple_followup_to_pro(self):
        history = [{"role": "user", "content": "请证明三角形内角和。"},
            {"role": "assistant", "content": "可作平行线证明。"},
            {"role": "user", "content": "平行线是什么意思？"}]
        self.assertEqual(route_model(history, False)[0], "deepseek-flash")

    def test_manual_model_wins_and_invalid_configuration_fails_closed(self):
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            with self.subTest(model=model), override_settings(DEEPSEEK_MODEL=model):
                self.assertEqual(self.route("请证明一个定理。", True), (model, "使用老师指定的模型"))
        with override_settings(DEEPSEEK_MODEL="unverified-model"), self.assertRaises(ChatError) as caught:
            self.route("你好")
        self.assertEqual(caught.exception.status, 503)


class AttachmentMessageTests(SimpleTestCase):
    def conversation(self, count):
        identifiers = [f"{index:032x}" for index in range(count)]
        result = []
        for offset in range(0, count, 3):
            if result:
                result.append({"role": "assistant", "content": "已说明"})
            result.append({"role": "user", "content": "分析这些附件", "attachments": identifiers[offset:offset + 3]})
        return result

    def test_twenty_distinct_attachments_allowed_but_twenty_one_rejected(self):
        messages = self.conversation(20)
        normalized, thinking = parse_messages({"messages": messages, "consent": True})
        self.assertEqual(normalized, messages)
        self.assertFalse(thinking)
        with self.assertRaises(ChatError) as error:
            parse_messages({"messages": self.conversation(21), "consent": True})
        self.assertEqual(error.exception.status, 400)

    def test_same_attachment_can_be_referenced_in_later_user_turn(self):
        identifier = "a" * 32
        messages = [{"role": "user", "content": "分析题目", "attachments": [identifier]},
            {"role": "assistant", "content": "先读题目。"},
            {"role": "user", "content": "再解释图表", "attachments": [identifier]}]
        normalized, _ = parse_messages({"messages": messages, "consent": True})
        self.assertEqual(normalized, messages)

    def test_any_historical_attachment_overrides_model_and_thinking_routing(self):
        messages = [{"role": "user", "content": "分析题图", "attachments": ["a" * 32]},
            {"role": "assistant", "content": "先读题。"}, {"role": "user", "content": "请证明结论。"}]
        for configured in ("auto", "deepseek-flash", "deepseek-v4-pro"):
            for thinking in (False, True):
                with self.subTest(configured=configured, thinking=thinking), override_settings(DEEPSEEK_MODEL=configured):
                    self.assertEqual(route_model(messages, thinking), ("deepseek-flash", "附件分析使用视觉模型"))
