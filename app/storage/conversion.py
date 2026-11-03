"""Launch an explicit, already-installed sandbox image from a dedicated worker only."""
import os
import subprocess
import uuid
from tempfile import TemporaryFile

from django.conf import settings

MAX_PDF = 32 * 1024 * 1024


def convert_office(data, suffix):
    if suffix not in (".doc", ".docx", ".ppt", ".pptx"):
        raise ValueError("Unsupported Office format.")
    image = os.getenv("TEACH_OFFICE_IMAGE")
    if not image or image.startswith("-"):
        raise RuntimeError("No approved Office renderer configured.")
    name = "teach-preview-" + uuid.uuid4().hex
    command = [
        "docker", "run", "--pull=never", "--rm", "--name", name, "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534", "--cpus=1",
        "--memory=768m", "--memory-swap=768m", "--pids-limit=64", "--ulimit=fsize=33554432:33554432",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777", "-i", image, suffix,
    ]
    with TemporaryFile(dir=settings.RUNTIME) as output:
        try:
            result = subprocess.run(command, input=data, stdout=output, stderr=subprocess.DEVNULL,
                                    timeout=90, check=False)
        except subprocess.TimeoutExpired:
            # Killing the CLI does not stop the conversion process in its container.
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=15)
            raise RuntimeError("Office rendering timed out.") from None
        if result.returncode or not 0 < output.tell() <= MAX_PDF:
            raise RuntimeError("Office rendering failed.")
        output.seek(0)
        pdf = output.read(MAX_PDF + 1)
    if not pdf.startswith(b"%PDF-"):
        raise ValueError("Renderer did not return a PDF.")
    return pdf
