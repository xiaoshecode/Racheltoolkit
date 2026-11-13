"""Private local-development credential storage, separate from teaching content."""
import contextlib
import os
from pathlib import Path
import re
import stat
import tempfile

MAX_KEY_BYTES = 1024


def validate_key(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_KEY_BYTES
            or re.search(r"[^\x21-\x7e]", value)):
        raise ValueError("API Key must be nonempty printable ASCII without whitespace.")
    return value


def read_key(runtime):
    path = Path(runtime) / "deepseek.key"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return ""
    except OSError:
        raise ValueError("Cannot read the private DeepSeek key file.") from None
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_KEY_BYTES + 1
                or (os.name == "posix" and (metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077))):
            raise ValueError("DeepSeek key file must be a private regular file owned by this user.")
        try:
            value = stream.read(MAX_KEY_BYTES + 2).decode("ascii").rstrip("\n")
        except UnicodeError:
            raise ValueError("Invalid private DeepSeek key file.") from None
    return validate_key(value)


def save_key(runtime, value):
    value = validate_key(value)
    directory = Path(runtime)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "deepseek.key"
    if destination.is_symlink():
        raise ValueError("DeepSeek key file cannot be a symbolic link.")
    descriptor, temporary = tempfile.mkstemp(prefix=".deepseek-", suffix=".key", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return destination


def local_key(runtime):
    """Environment wins; production and Codespaces continue using managed secrets."""
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if value or os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings") != "config.settings":
        return value
    return read_key(runtime)
