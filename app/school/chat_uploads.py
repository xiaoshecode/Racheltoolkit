"""Validate opt-in chat attachments and proxy them to the fixed DeepSeek Files API."""
import contextlib
import http.client
import io
import json
import re
import secrets
import socket
import threading
import time
import uuid
import warnings
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from django.core.exceptions import RequestDataTooBig, TooManyFilesSent
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_POST
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from .permissions import assistant_access

MAX_UPLOAD = 20 * 1024 * 1024
MAX_TEXT = 1024 * 1024
MAX_SESSION_ATTACHMENTS = 20
SESSION_KEY = "assistant_attachments"
IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".gif": "GIF", ".webp": "WEBP"}
TEXT_FORMATS = {".txt", ".md", ".csv", ".json"}
ZIP_PARTS = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml", ".xlsx": "xl/workbook.xml"}
OLE_FORMATS = {".doc", ".ppt", ".xls"}


class UploadError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def validate_attachment(upload):
    """Do bounded structural checks; never unpack documents or run their contents."""
    name = str(upload.name).replace("\\", "/").rsplit("/", 1)[-1]
    try:
        encoded_name = name.encode("utf-8")
    except UnicodeError:
        raise UploadError("附件文件名含有无效字符，请重命名后重新选择。") from None
    if (not name or len(encoded_name) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise UploadError("附件文件名无效，请重命名后重新选择。")
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in set(IMAGE_FORMATS) | TEXT_FORMATS | set(ZIP_PARTS) | OLE_FORMATS | {".pdf"}:
        raise UploadError("暂不支持这个附件格式，请选择图片、PDF、Office 或文本文件。")
    data = upload.read(MAX_UPLOAD + 1)
    if not data or len(data) > MAX_UPLOAD or len(data) != upload.size:
        raise UploadError("请选择非空附件，每次上传总计不超过 20 MiB。", 413)
    try:
        if suffix in IMAGE_FORMATS:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != IMAGE_FORMATS[suffix] or max(image.size) > 8192:
                        raise ValueError("image format or dimensions")
                    image.verify()
        elif suffix == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise ValueError("pdf header")
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted or not 0 < len(reader.pages) <= 1000:
                raise ValueError("pdf encrypted or pages")
        elif suffix in ZIP_PARTS:
            with ZipFile(io.BytesIO(data)) as package:
                entries = package.infolist()
                if len(entries) > 5000 or sum(entry.file_size for entry in entries) > 100 * 1024 * 1024:
                    raise ValueError("zip size")
                for entry in entries:
                    normalized = entry.filename.replace("\\", "/")
                    if (normalized.startswith("/") or ".." in PurePosixPath(normalized).parts
                            or re.match(r"^[A-Za-z]:", normalized) or entry.flag_bits & 1
                            or entry.file_size > 50 * 1024 * 1024):
                        raise ValueError("zip path or encrypted entry")
                required = {"[Content_Types].xml", ZIP_PARTS[suffix]}
                if not required.issubset(package.namelist()):
                    raise ValueError("office parts")
        elif suffix in OLE_FORMATS:
            if not data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")) or len(data) < 512:
                raise ValueError("ole header")
        else:
            if len(data) > MAX_TEXT:
                raise UploadError("文本附件请控制在 1 MiB 以内，较长材料可转为 PDF。", 413)
            text = data.decode("utf-8-sig")
            if not text.strip() or any(ord(char) < 32 and char not in "\t\r\n" for char in text):
                raise ValueError("text content")
            if suffix == ".json":
                json.loads(text)
    except UploadError:
        raise
    except (ValueError, UnicodeError, OSError, BadZipFile, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning, RecursionError):
        raise UploadError("附件无法读取、设有密码或内容与格式不符，请检查后重新上传。") from None
    except Exception:
        # PDF parsers can emit content or file locations; never return parser diagnostics.
        raise UploadError("附件无法读取，请检查文件或转换为 PDF 后重新上传。") from None
    purpose = "rich_layout_recognition" if suffix in {".pdf", ".doc", ".docx", ".ppt", ".pptx"} else "user_data"
    return {"name": name, "size": len(data), "data": data, "purpose": purpose}


def _multipart(attachment):
    boundary = "teach-" + secrets.token_hex(24)
    parts = []
    for name, value in (("purpose", attachment["purpose"]),
                        ("expires_after[anchor]", "created_at"),
                        ("expires_after[seconds]", "3600")):
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
    filename = attachment["name"].replace('"', "_")
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8"))
    parts.extend((attachment["data"], f"\r\n--{boundary}--\r\n".encode()))
    return b"".join(parts), boundary


@sensitive_variables("attachment", "body", "headers", "key", "value")
def upload_to_provider(attachment, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UploadError("附件处理等待超时，请稍后重新上传。", 504)
    connection = http.client.HTTPSConnection("api.deepseek.com", timeout=min(60, remaining))
    timer = response = None
    try:
        connection.connect()
        upstream_socket = connection.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UploadError("附件处理等待超时，请稍后重新上传。", 504)
        upstream_socket.settimeout(min(240, remaining))

        def interrupt():
            with contextlib.suppress(OSError):
                upstream_socket.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                connection.close()

        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        body, boundary = _multipart(attachment)
        key = settings.DEEPSEEK_API_KEY
        headers = {"Authorization": f"Bearer {key}",
                   "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"}
        # Fixed host and path: credentials can never follow an upstream redirect.
        connection.request("POST", "/files", body=body, headers=headers)
        response = connection.getresponse()
        if response.status not in (200, 201):
            if response.status == 429:
                raise UploadError("附件分析服务正在处理较多文件，请稍后再试。", 429)
            if response.status in (401, 402, 403):
                raise UploadError("附件分析暂时不可用，请联系老师检查服务状态。", 503)
            if response.status in (408, 504):
                raise UploadError("附件处理等待超时，请缩小文件后重试。", 504)
            if response.status in (400, 413, 422):
                raise UploadError("附件未能解析，请检查格式、密码和大小后重新上传。", 422)
            raise UploadError("附件分析服务暂不可用，请稍后再试。", 503)
        if response.getheader("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise UploadError("附件服务返回异常，请稍后再试。", 502)
        raw = response.read(65537)
        if len(raw) > 65536:
            raise UploadError("附件服务返回异常，请稍后再试。", 502)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise UploadError("附件服务返回异常，请稍后再试。", 502) from None
        provider_id = value.get("id") if isinstance(value, dict) else None
        expires_at = value.get("expires_at") if isinstance(value, dict) else None
        if (not isinstance(provider_id, str) or not re.fullmatch(r"file-api-[A-Za-z0-9_-]{1,160}", provider_id)
                or type(expires_at) is not int or not time.time() < expires_at <= time.time() + 3660):
            raise UploadError("附件服务未确认有效文件，请稍后再试。", 502)
        return provider_id, expires_at
    except (OSError, http.client.HTTPException):
        raise UploadError("附件上传或处理未能完成，请稍后检查并重新上传。", 502) from None
    finally:
        if timer is not None:
            timer.cancel()
        with contextlib.suppress(OSError, http.client.HTTPException):
            if response is not None:
                response.close()
        with contextlib.suppress(OSError, http.client.HTTPException):
            connection.close()


@never_cache
@csrf_protect
@require_POST
def upload_attachments(request):
    from .chat import ChatError, Lease, configured

    if not request.user.is_authenticated:
        return JsonResponse({"error": "登录已过期，请重新登录后再试。"}, status=401)
    if not assistant_access(request.user):
        return JsonResponse({"error": "加入教学班级后即可使用学习助手。"}, status=403)
    if not configured():
        return JsonResponse({"error": "老师尚未开启学习助手，请稍后再来。"}, status=503)
    completed, lease = [], None
    try:
        if request.content_type != "multipart/form-data" or request.POST.get("consent") != "true":
            raise UploadError("请先同意将所选附件发送给 DeepSeek 处理。", 400)
        files = request.FILES.getlist("files")
        if set(request.FILES) != {"files"} or not 1 <= len(files) <= 3:
            raise UploadError("每次请选择 1 至 3 个附件。", 400)
        if sum(upload.size for upload in files) > MAX_UPLOAD:
            raise UploadError("每次上传的附件总计不能超过 20 MiB。", 413)
        # Validate the full selection before sending even the first file to DeepSeek.
        attachments = [validate_attachment(upload) for upload in files]
        now = time.time()
        existing = request.session.get(SESSION_KEY, {})
        mapping = {key: value for key, value in existing.items() if isinstance(value, dict)
                   and type(value.get("expires_at")) is int and value["expires_at"] > now} if isinstance(existing, dict) else {}
        if len(mapping) + len(attachments) > MAX_SESSION_ATTACHMENTS:
            raise UploadError("当前会话附件数量已达上限，请稍后再上传。", 429)
        lease = Lease(request.user.pk, duration=300)
        for attachment in attachments:
            provider_id, expires_at = upload_to_provider(attachment, lease.deadline)
            local_id = uuid.uuid4().hex
            mapping[local_id] = {"provider_id": provider_id, "name": attachment["name"],
                                 "size": attachment["size"], "expires_at": expires_at}
            request.session[SESSION_KEY] = mapping.copy()
            # Persist each success immediately, including if a later upload fails.
            request.session.save()
            completed.append({"id": local_id, "name": attachment["name"],
                              "size": attachment["size"], "expires_at": expires_at})
        return JsonResponse({"attachments": completed})
    except (RequestDataTooBig, TooManyFilesSent):
        return JsonResponse({"error": "附件数量或大小超限，请减少附件后重试。", "attachments": completed}, status=413)
    except (UploadError, ChatError) as error:
        result = JsonResponse({"error": str(error), "attachments": completed}, status=error.status)
        if error.status == 429:
            result["Retry-After"] = "60"
        return result
    except (OSError, http.client.HTTPException):
        return JsonResponse({"error": "附件服务暂不可用，请稍后再试。", "attachments": completed}, status=503)
    finally:
        if lease is not None:
            lease.close()
