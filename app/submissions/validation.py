import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree

import olefile
from PIL import Image
from pypdf import PdfReader

LIMIT = 20 * 1024 * 1024


class Refused(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass
class Item:
    name: str
    data: bytes
    sha: str


def detect(data, name):
    suffix = Path(name).suffix.lower()
    stream = BytesIO(data)
    try:
        if suffix in {".jpg", ".jpeg", ".png"}:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(stream) as picture:
                    if (
                        picture.format != ("PNG" if suffix == ".png" else "JPEG")
                        or picture.width * picture.height > 40_000_000
                    ):
                        raise ValueError()
                    picture.verify()
                stream.seek(0)
                with Image.open(stream) as picture:
                    picture.load()
            return "photo"
        if suffix == ".pdf" and data.startswith(b"%PDF-") and len(PdfReader(stream).pages):
            return "document"
        if suffix == ".docx":
            with ZipFile(stream) as doc:
                types = doc.getinfo("[Content_Types].xml")
                body = doc.getinfo("word/document.xml")
                if types.file_size > 512 * 1024 or body.file_size > 8 * 1024 * 1024:
                    raise ValueError()
                if not any(
                    e.get("ContentType")
                    == "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
                    for e in ElementTree.fromstring(doc.read(types))
                ):
                    raise ValueError()
                if (
                    ElementTree.fromstring(doc.read(body)).tag
                    != "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document"
                ):
                    raise ValueError()
            return "document"
        if suffix == ".doc" and olefile.isOleFile(stream):
            with olefile.OleFileIO(stream) as doc:
                if (
                    doc.exists("WordDocument")
                    and doc.openstream("WordDocument").read(2) == b"\xec\xa5"
                ):
                    return "document"
    except Exception as exc:
        raise Refused("文件结构损坏或与扩展名不符。") from exc
    raise Refused("仅接受 JPEG、PNG、PDF、DOC 或 DOCX 原件。")


def validate(files):
    if not 1 <= len(files) <= 10:
        raise Refused("请选择 1–10 张照片或一份文档。")
    if sum(item.size for item in files) > LIMIT:
        raise Refused("附件合计超过 20 MiB。", 413)
    result = []
    total = 0
    for item in files:
        data = item.read(LIMIT - total + 1)
        total += len(data)
        if total > LIMIT:
            raise Refused("附件合计超过 20 MiB。", 413)
        if not data or not item.name or any(ord(c) < 32 for c in item.name):
            raise Refused("文件为空或文件名无效。")
        if detect(data, item.name) != "photo" and len(files) != 1:
            raise Refused("一份文档须单独提交。")
        result.append(Item(item.name, data, hashlib.sha256(data).hexdigest()))
    return result
