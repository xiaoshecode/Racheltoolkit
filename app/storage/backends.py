"""Private immutable object storage. A missing object and an unavailable store are distinct."""
import http.client
import json
import os
import posixpath
import re
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


DIRECTORIES = {"course": "公开课件", "submission": "学生作业", "answer": "教师备课",
               "preview": "教师备课", "archive": "教学归档"}
CHUNK = 256 * 1024


class StorageError(Exception):
    pass


class StorageUnavailable(StorageError):
    """No usable response for a read, or bytes definitely not sent."""


class WriteUncertain(StorageError):
    """Upload was dispatched; reconcile bytes before retrying."""


class ContentConflict(StorageError):
    """Bytes do not match the registered size and hash."""


def validate_path(path):
    parts = path.split("/")
    if (len(parts) != 3 or parts[0] or parts[1] not in DIRECTORIES.values()
            or not re.fullmatch(r"[0-9a-f]{32}(?:\.[a-z0-9]{1,8})?", parts[2])):
        raise StorageUnavailable("Invalid storage object location.")


class LocalBackend:
    def __init__(self, root=None):
        self.root = Path(root or getattr(settings, "LOCAL_STORAGE_ROOT", settings.RUNTIME / "private/storage"))

    def filename(self, path):
        validate_path(path)
        return self.root.joinpath(*path.lstrip("/").split("/"))

    def open(self, path):
        try:
            return self.filename(path).open("rb")
        except FileNotFoundError:
            return None
        except OSError:
            raise StorageUnavailable("Local object could not be opened.") from None

    def put(self, path, data):
        target = self.filename(path)
        tmp = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=target.parent, delete=False) as out:
                tmp = Path(out.name)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            # link is atomic and cannot overwrite another claimant's file.
            try:
                os.link(tmp, target)
            except FileExistsError:
                pass
        except OSError:
            raise StorageUnavailable("Local write failed.") from None
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)


class FaultBackend(LocalBackend):
    """One-shot fault injection; a fresh instance sees persisted bytes after restart."""
    def __init__(self, root, failure):
        super().__init__(root)
        self.failure = failure

    def put(self, path, data):
        failure, self.failure = self.failure, None
        if failure == "before_write":
            raise StorageUnavailable("Injected failure before upload.")
        super().put(path, data)
        if failure == "after_write":
            raise WriteUncertain("Injected failure after upload.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def origin(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in url
                or any(ord(c) < 33 or ord(c) == 127 for c in url)):
            raise ValueError
        return parsed.hostname.lower(), parsed.port or 443
    except (ValueError, UnicodeError):
        raise StorageUnavailable("Invalid storage service URL.") from None


class SeafileBackend:
    """Repository token goes only to the API. Transfer links never leave this adapter."""
    def __init__(self, base, token, transfer_origins=()):
        self.base = base.rstrip("/")
        self.token = token
        self.origins = {origin(self.base)}
        if urlsplit(self.base).query or not token:
            raise ImproperlyConfigured("Seafile requires a base URL and repository token.")
        for host in transfer_origins:
            if urlsplit(host).path not in ("", "/") or urlsplit(host).query:
                raise ImproperlyConfigured("Transfer origins cannot contain paths or queries.")
            self.origins.add(origin(host))
        self.opener = build_opener(NoRedirect())

    def request(self, request, *, dispatched=False):
        try:
            return self.opener.open(request, timeout=30)
        except (HTTPError, OSError, ValueError, http.client.HTTPException) as exc:
            if isinstance(exc, HTTPError):
                exc.close()
            error = WriteUncertain if dispatched else StorageUnavailable
            raise error("Cloud transfer did not complete.") from None

    def decode(self, response, *, dispatched=False):
        error = WriteUncertain if dispatched else StorageUnavailable
        try:
            with response:
                data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError
            return json.loads(data)
        except (ValueError, UnicodeError, OSError, http.client.HTTPException):
            raise error("Invalid response from the file service.") from None

    def api(self, endpoint, path, **params):
        url = self.base + "/api/v2.1/via-repo-token/" + endpoint + "/?" + urlencode({"path": path, **params})
        req = Request(url, headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"})
        return self.decode(self.request(req))

    def link(self, endpoint, path, **params):
        link = self.api(endpoint, path, **params)
        if not isinstance(link, str) or origin(link) not in self.origins:
            raise StorageUnavailable("Untrusted transfer origin.")
        return link

    def open(self, path):
        validate_path(path)
        directory, name = posixpath.split(path)
        # A revoked token, missing library, or gateway's 404 is not proof of absence.
        listing = self.api("dir", directory)
        entries = listing.get("dirent_list") if isinstance(listing, dict) else None
        if not isinstance(entries, list) or any(not isinstance(x, dict) or not isinstance(x.get("name"), str)
                                                for x in entries):
            raise StorageUnavailable("Could not inspect the private directory.")
        if not any(x["name"] == name for x in entries):
            return None
        return self.request(Request(self.link("download-link", path)))

    def put(self, path, data):
        validate_path(path)
        directory, name = posixpath.split(path)
        url = self.link("upload-link", directory, replace="0")
        url += ("&" if urlsplit(url).query else "?") + "ret-json=1"
        boundary = secrets.token_hex(24)
        fields = (f'--{boundary}\r\nContent-Disposition: form-data; name="parent_dir"\r\n\r\n{directory}\r\n'
                  f'--{boundary}\r\nContent-Disposition: form-data; name="replace"\r\n\r\n0\r\n'
                  f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n')
        body = fields.encode() + data + f"\r\n--{boundary}--\r\n".encode()
        req = Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        result = self.decode(self.request(req, dispatched=True), dispatched=True)
        if (not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict)
                or result[0].get("name") != name):
            # replace=0 may return "name (1)". Never adopt or delete that renamed file.
            raise WriteUncertain("Server did not confirm the exact reserved filename.")


def backend_name():
    return getattr(settings, "TEACH_STORAGE_BACKEND", os.getenv("TEACH_STORAGE_BACKEND", "local"))


def library_id():
    if backend_name() == "local":
        marker = settings.RUNTIME / "restored-library.txt"
        if marker.is_file():
            return marker.read_text(encoding="utf-8").strip()
        return "local"
    value = os.getenv("SEAFILE_LIBRARY_ID", "").strip()
    if backend_name() != "seafile" or not value:
        raise ImproperlyConfigured("Set the Seafile library identity for cloud storage.")
    return value


def get_backend():
    if backend_name() == "local":
        return LocalBackend()
    library_id()
    return SeafileBackend(os.getenv("SEAFILE_BASE_URL", ""), os.getenv("SEAFILE_REPO_TOKEN", ""),
                          filter(None, (s.strip() for s in os.getenv("SEAFILE_TRANSFER_ORIGINS", "").split(","))))
