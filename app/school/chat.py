"""Opt-in, authenticated DeepSeek streaming; no course data or chat text is stored."""
import contextlib
import http.client
import json
import math
import re
import secrets
import socket
import sqlite3
import threading
import time

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from .permissions import assistant_access

MAX_BODY = 120_000
MAX_LINE = 262_144
MAX_HISTORY = 24_000
MAX_OUTPUT = 120_000
MAX_DURATION = 120
SYSTEM_MESSAGE = (
    "你是吉老师个人教学空间中的 AI 学习助手。使用清晰的中文，帮助学生理解概念、"
    "分析解题思路，也帮助老师准备教学内容。优先引导思考，必要时给出分步说明。"
    "如果题目信息不足，请先询问；不确定时明确说明。你无法读取站内课程文件、"
    "学生作业或成绩，除非用户在对话中提供内容。数学公式尽量使用易读的文本。"
)


class ChatError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class Lease:
    """Share spending limits across WSGI workers without storing prompts or replies."""
    def __init__(self, user_id, duration=MAX_DURATION):
        self.user_id = user_id
        self.token = secrets.token_hex(16)
        self.closed = False
        self.deadline = time.monotonic() + duration
        self.database = settings.RUNTIME / "assistant-limits.sqlite3"
        now = time.time()
        try:
            with contextlib.closing(sqlite3.connect(self.database, timeout=2)) as database, database:
                database.execute("BEGIN IMMEDIATE")
                database.execute("""CREATE TABLE IF NOT EXISTS requests (
                    user_id INTEGER PRIMARY KEY, window_start REAL NOT NULL,
                    request_count INTEGER NOT NULL, lease TEXT, lease_until REAL NOT NULL
                )""")
                database.execute(
                    "DELETE FROM requests WHERE window_start < ? AND lease_until < ?",
                    (now - 86400, now),
                )
                previous = database.execute(
                    "SELECT window_start, request_count, lease_until FROM requests WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if previous and previous[2] > now:
                    raise ChatError("上一条回复仍在生成，请稍后再试。", 429)
                start, count = (previous[0], previous[1]) if previous and previous[0] > now - 60 else (now, 0)
                if count >= 8:
                    raise ChatError("发送得太快了，请一分钟后再试。", 429)
                active = database.execute(
                    "SELECT COUNT(*) FROM requests WHERE lease_until > ?", (now,),
                ).fetchone()[0]
                if active >= 4:
                    raise ChatError("学习助手正在回答其他问题，请稍后再试。", 429)
                database.execute(
                    "INSERT OR REPLACE INTO requests VALUES (?, ?, ?, ?, ?)",
                    (user_id, start, count + 1, self.token, now + duration + 15),
                )
        except sqlite3.Error:
            raise ChatError("学习助手暂时无法接收请求，请稍后再试。", 503) from None

    def close(self):
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(sqlite3.Error):
            with contextlib.closing(sqlite3.connect(self.database, timeout=2)) as database, database:
                database.execute(
                    "UPDATE requests SET lease = NULL, lease_until = 0 WHERE user_id = ? AND lease = ?",
                    (self.user_id, self.token),
                )


def parse_messages(payload):
    if not isinstance(payload, dict) or set(payload) - {"messages", "thinking", "consent"}:
        raise ChatError("对话请求格式不正确，请刷新后再试。", 400)
    # No data leaves the server until the user explicitly agrees to this destination.
    if payload.get("consent") is not True:
        raise ChatError("请先确认同意将当前对话发送给 DeepSeek。", 400)
    thinking = payload.get("thinking", False)
    messages = payload.get("messages")
    if type(thinking) is not bool or not isinstance(messages, list) or not 1 <= len(messages) <= 20:
        raise ChatError("对话过长或格式不正确，请开启新对话。", 400)
    cleaned, total, all_attachments = [], 0, set()
    for index, message in enumerate(messages):
        expected = "user" if index % 2 == 0 else "assistant"
        allowed = {"role", "content", "attachments"} if expected == "user" else {"role", "content"}
        if (not isinstance(message, dict) or not {"role", "content"}.issubset(message)
                or set(message) - allowed or message.get("role") != expected
                or not isinstance(message.get("content"), str)):
            raise ChatError("对话顺序不正确，请开启新对话。", 400)
        attachments = message.get("attachments")
        if "attachments" in message:
            if (not isinstance(attachments, list) or not 1 <= len(attachments) <= 3
                    or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value)
                           for value in attachments) or len(set(attachments)) != len(attachments)):
                raise ChatError("附件引用无效，请重新选择附件。", 400)
            all_attachments.update(attachments)
            if len(all_attachments) > 20:
                raise ChatError("本轮引用的附件过多，请开启新对话。", 400)
        content = message["content"]
        try:
            content.encode("utf-8")
        except UnicodeError:
            raise ChatError("问题中包含无效字符，请重新输入。", 400) from None
        if not content.strip() or len(content) > (4000 if expected == "user" else 16000):
            raise ChatError("每次提问请控制在 4000 字以内；较长对话请重新开始。", 400)
        total += len(content)
        cleaned.append({"role": expected, "content": content})
        if attachments:
            cleaned[-1]["attachments"] = attachments.copy()
    if cleaned[-1]["role"] != "user" or total > MAX_HISTORY:
        raise ChatError("对话过长或尚未输入问题，请开启新对话。", 400)
    return cleaned, thinking


def provider_messages(messages, session_attachments=None):
    """Resolve opted-in session-owned references; never trust client provider IDs."""
    mapping = session_attachments if isinstance(session_attachments, dict) else {}
    now = time.time()
    result = []
    for message in messages:
        identifiers = message.get("attachments", [])
        content = message["content"]
        if identifiers:
            parts = []
            for identifier in identifiers:
                attachment = mapping.get(identifier)
                if not isinstance(attachment, dict):
                    raise ChatError("附件不属于当前登录会话，请重新上传。", 403)
                expires_at = attachment.get("expires_at")
                if (type(expires_at) not in (int, float) or not math.isfinite(expires_at)
                        or expires_at <= now):
                    raise ChatError("附件已过期，请重新上传后再试。", 400)
                provider_id = attachment.get("provider_id")
                if (not isinstance(provider_id, str)
                        or not re.fullmatch(r"file-api-[A-Za-z0-9_-]{1,160}", provider_id)):
                    raise ChatError("附件记录无效，请重新上传。", 400)
                parts.append({"type": "file", "file_id": provider_id})
            parts.append({"type": "text", "text": content})
            content = parts
        result.append({"role": message["role"], "content": content})
    return result


def configured():
    key = settings.DEEPSEEK_API_KEY
    return bool(key and not re.search(r"[^\x21-\x7e]", key)
                and settings.DEEPSEEK_MODEL in {"auto", "deepseek-flash", "deepseek-v4-pro"})


PROOF_REQUEST = re.compile(
    r"(?:^|[\n。！？；;，,:：])\s*(?:(?:请|试|求|帮我|严格|如何|怎样|给出|完成)\s*)?"
    r"(?:证明|推导)(?!是什么|是指|的|方法|技巧|题|思路)"
    r"|(?:请|试|求|帮我|需要|要求|严格|如何|怎样|给出|完成)\s*"
    r"(?:证明|推导)(?!是什么|是指|的|方法|技巧|题|思路)"
    r"|\b(?:prove|derive)\b|\brigorous\s+proof\b",
    re.IGNORECASE,
)
CONDITION_MARKERS = (
    re.compile(r"已知|给定|假设|\bgiven\b|\bassume\b", re.IGNORECASE),
    re.compile(r"满足|约束|限制|条件|\bconstraints?\b|\bsubject\s+to\b", re.IGNORECASE),
    re.compile(r"同时|并且|且|以及|\band\b", re.IGNORECASE),
    re.compile(r"至少|至多|不超过|不小于|不大于|\bat\s+(?:least|most)\b", re.IGNORECASE),
    re.compile(r"分别|对所有|任意|对于每|\bfor\s+(?:all|each)\b", re.IGNORECASE),
    re.compile(r"最优|最大值|最小值|极值|\b(?:optimi[sz]e|minimi[sz]e|maximi[sz]e)\b", re.IGNORECASE),
)
NUMBERED_CONDITIONS = re.compile(r"(?:^|\n)\s*(?:\d+[.)、]|[（(]\d+[）)]|[①②③④⑤⑥⑦⑧⑨⑩])")


def route_model(messages, thinking):
    """Route locally using explicit task and size signals; no classifier request."""
    if any(message.get("attachments") for message in messages):
        return "deepseek-flash", "附件分析使用视觉模型"
    selected = settings.DEEPSEEK_MODEL
    if selected in {"deepseek-flash", "deepseek-v4-pro"}:
        return selected, "使用老师指定的模型"
    if selected != "auto":
        raise ChatError("学习助手的模型配置无效，请联系老师。", 503)
    if thinking:
        return "deepseek-v4-pro", "深度思考已开启"
    question = messages[-1]["content"]
    if sum(len(message["content"]) for message in messages) >= 8000:
        return "deepseek-v4-pro", "对话上下文较长"
    if len(question) >= 1200:
        return "deepseek-v4-pro", "当前问题较长"
    if PROOF_REQUEST.search(question):
        return "deepseek-v4-pro", "问题包含证明或推导任务"
    if len(question) >= 180 and (
        len(NUMBERED_CONDITIONS.findall(question)) >= 3
        or sum(bool(marker.search(question)) for marker in CONDITION_MARKERS) >= 3
    ):
        return "deepseek-v4-pro", "问题包含多项条件或约束"
    return "deepseek-flash", "常规学习问题"


def event(kind, **fields):
    return "data: " + json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n\n"


def provider_events(response, deadline):
    data = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise ChatError("回复等待时间过长，请缩短问题后重试。", 504)
        line = response.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE:
            raise ChatError("回复格式异常，请稍后重试。")
        if not line:
            raise ChatError("回复连接中断，请重试当前问题。")
        try:
            line = line.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise ChatError("回复格式异常，请稍后重试。") from None
        if line.startswith(":"):
            yield "keep-alive"
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
            size += len(line)
            if size > MAX_LINE:
                raise ChatError("回复格式异常，请稍后重试。")
        elif not line and data:
            text = "\n".join(data)
            data, size = [], 0
            if text == "[DONE]":
                return
            try:
                value = json.loads(text)
            except (ValueError, RecursionError):
                raise ChatError("回复格式异常，请稍后重试。") from None
            if not isinstance(value, dict) or "error" in value:
                raise ChatError("DeepSeek 暂时无法完成回复，请稍后重试。")
            yield value


class ChatStream:
    def __init__(self, connection, response, timer, deadline, lease, model, reason):
        self.connection = connection
        self.response = response
        self.timer = timer
        self.deadline = deadline
        self.lease = lease
        self.model = model
        self.reason = reason
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.timer.cancel()
        try:
            with contextlib.suppress(OSError, http.client.HTTPException):
                self.response.close()
            with contextlib.suppress(OSError, http.client.HTTPException):
                self.connection.close()
        finally:
            self.lease.close()

    def __iter__(self):
        finish = None
        total = 0
        has_content = False
        try:
            yield event("model", model=self.model, reason=self.reason)
            for chunk in provider_events(self.response, self.deadline):
                if chunk == "keep-alive":
                    yield ": keep-alive\n\n"
                    continue
                choices = chunk.get("choices", [])
                if not isinstance(choices, list):
                    raise ChatError("回复格式异常，请稍后重试。")
                if not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise ChatError("回复格式异常，请稍后重试。")
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise ChatError("回复格式异常，请稍后重试。")
                for source, kind in (("reasoning_content", "reasoning"), ("content", "content")):
                    content = delta.get(source)
                    if content is None or content == "":
                        continue
                    if not isinstance(content, str):
                        raise ChatError("回复格式异常，请稍后重试。")
                    try:
                        content.encode("utf-8")
                    except UnicodeError:
                        raise ChatError("回复格式异常，请稍后重试。") from None
                    total += len(content)
                    if total > MAX_OUTPUT:
                        raise ChatError("回复过长，请拆分问题后重试。")
                    if kind == "content":
                        has_content = True
                    yield event(kind, content=content)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            if finish not in ("stop", "length"):
                raise ChatError("本次回复未完成，请调整问题后重试。")
            if not has_content:
                raise ChatError("本次思考未形成回复，请关闭深度思考或缩短问题后重试。")
            yield event("done", finish_reason=finish)
        except ChatError as error:
            yield event("error", message=str(error))
        except (OSError, http.client.HTTPException):
            yield event("error", message="回复连接中断或等待超时，请重试当前问题。")
        finally:
            self.close()


@sensitive_variables("key", "headers", "body")
def open_stream(messages, thinking, lease, session_attachments=None):
    key = settings.DEEPSEEK_API_KEY
    model, reason = route_model(messages, thinking)
    resolved_messages = provider_messages(messages, session_attachments)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_MESSAGE}, *resolved_messages],
        "stream": True,
        "thinking": {"type": "enabled" if thinking else "disabled"},
        "max_tokens": settings.DEEPSEEK_MAX_TOKENS,
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "text/event-stream"}
    connection = http.client.HTTPSConnection("api.deepseek.com", timeout=settings.DEEPSEEK_TIMEOUT_SECONDS)
    response, timer = None, None
    try:
        connection.connect()
        upstream_socket = connection.sock

        def interrupt():
            with contextlib.suppress(OSError):
                upstream_socket.shutdown(socket.SHUT_RDWR)
            connection.close()

        deadline = lease.deadline
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChatError("连接等待时间过长，请稍后重试。", 504)
        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        # Fixed HTTPS host/path: redirects never forward server credentials.
        connection.request("POST", "/chat/completions", body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            status = response.status
            if status in (401, 402, 403):
                raise ChatError("学习助手暂时不可用，请联系老师检查服务配置或额度。", 503)
            if status == 429:
                raise ChatError("DeepSeek 当前请求较多，请稍后再试。", 429)
            raise ChatError("DeepSeek 暂时无法完成回复，请稍后再试。")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip() != "text/event-stream":
            raise ChatError("回复格式异常，请稍后重试。")
        return ChatStream(connection, response, timer, deadline, lease, model, reason)
    except BaseException:
        if timer is not None:
            timer.cancel()
        with contextlib.suppress(OSError, http.client.HTTPException):
            if response is not None:
                response.close()
        with contextlib.suppress(OSError, http.client.HTTPException):
            connection.close()
        raise


@never_cache
@login_required
@require_GET
def assistant(request):
    if not assistant_access(request.user):
        return render(request, "school/chat.html", {"assistant_enabled": False,
            "assistant_denied": True}, status=403)
    return render(request, "school/chat.html", {"assistant_enabled": configured(),
        "assistant_model": settings.DEEPSEEK_MODEL})


@never_cache
@require_POST
def chat_completion(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "登录已过期，请重新登录后再试。"}, status=401)
    if not assistant_access(request.user):
        return JsonResponse({"error": "加入教学班级后即可使用学习助手。"}, status=403)
    if not configured():
        return JsonResponse({"error": "老师尚未开启学习助手，请稍后再来。"}, status=503)
    lease = None
    try:
        if request.content_type != "application/json":
            raise ChatError("请使用网页对话框发送问题。", 400)
        if len(request.body) > MAX_BODY:
            raise ChatError("对话过长，请开启新对话。", 413)
        try:
            payload = json.loads(request.body)
        except (ValueError, UnicodeError, RecursionError):
            raise ChatError("对话请求格式不正确，请刷新后再试。", 400) from None
        messages, thinking = parse_messages(payload)
        session_attachments = request.session.get("assistant_attachments", {})
        provider_messages(messages, session_attachments)
        lease = Lease(request.user.pk)
        stream = open_stream(messages, thinking, lease, session_attachments)
    except RequestDataTooBig:
        return JsonResponse({"error": "对话过长，请开启新对话。"}, status=413)
    except ChatError as error:
        if lease:
            lease.close()
        response = JsonResponse({"error": str(error)}, status=error.status)
        if error.status == 429:
            response["Retry-After"] = "60"
        return response
    except (OSError, http.client.HTTPException):
        if lease:
            lease.close()
        return JsonResponse({"error": "暂时无法连接 DeepSeek，请稍后再试。"}, status=502)
    response = StreamingHttpResponse(stream, content_type="text/event-stream; charset=utf-8")
    response["X-Accel-Buffering"] = "no"
    response["X-Content-Type-Options"] = "nosniff"
    return response
