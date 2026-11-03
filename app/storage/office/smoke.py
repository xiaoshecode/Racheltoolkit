"""Run with locked app dependencies and the built converter on a Linux worker."""
import io
import os
import sys
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from pypdf import PdfReader
from storage.conversion import convert_office


def synthetic_docx():
    buf = io.BytesIO()
    parts = {
        "[Content_Types].xml": '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
        <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
        <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>''',
        "_rels/.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
        <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''',
        "word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:body><w:p><w:r><w:t>Classroom fixture page one</w:t><w:br w:type="page"/>
        <w:t>Classroom fixture page two</w:t></w:r></w:p></w:body></w:document>''',
    }
    with ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


if __name__ == "__main__":
    result = convert_office(synthetic_docx(), ".docx")
    reader = PdfReader(io.BytesIO(result))
    if len(reader.pages) != 2 or "page two" not in reader.pages[1].extract_text():
        raise RuntimeError("Two-page Word fixture did not survive conversion")
    print(f"PASS sandboxed DOCX to PDF: pages=2 bytes={len(result)}")
