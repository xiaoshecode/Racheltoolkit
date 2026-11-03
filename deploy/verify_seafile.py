"""Check one Seafile library using synthetic data and an API token."""

import argparse
import hashlib
import http.client
import json
import os
import posixpath
import secrets
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ProbeError(Exception):
    """Message safe to show without revealing tokens, signed URLs or remote data."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_origin(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in url
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
            raise ValueError
        return parsed.hostname, parsed.port or 443
    except (ValueError, UnicodeError):
        raise ProbeError("Expected a valid HTTPS URL without credentials or fragments.") from None


def request(step, url, *, headers=None, method="GET", data=None, limit=4 * 1024 * 1024):
    try:
        req = Request(url, data=data, headers=headers or {}, method=method)
        with build_opener(NoRedirect()).open(req, timeout=30) as response:
            body = response.read(limit + 1)
    except HTTPError as exc:
        exc.close()
        raise ProbeError(f"{step}: HTTP {exc.code}.") from None
    except (OSError, http.client.HTTPException, ValueError):
        raise ProbeError(f"{step}: connection, TLS or response failed.") from None
    if len(body) > limit:
        raise ProbeError(f"{step}: response exceeds probe size limit.")
    return body


def parse_json(step, body):
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise ProbeError(f"{step}: expected a JSON response.") from None


def multipart(directory, name, contents):
    boundary = "SeafileProbe" + secrets.token_hex(16)
    fields = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="parent_dir"\r\n\r\n{directory}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="replace"\r\n\r\n0\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        'Content-Type: text/plain\r\n\r\n'
    )
    body = fields.encode("utf-8") + contents + f"\r\n--{boundary}--\r\n".encode("ascii")
    return {"Content-Type": f"multipart/form-data; boundary={boundary}"}, body


def run(args):
    base = os.getenv("SEAFILE_BASE_URL", "").strip().rstrip("/")
    token = os.getenv("SEAFILE_REPO_TOKEN", "").strip()
    library = os.getenv("SEAFILE_LIBRARY_ID", "").strip()
    if not base or not token or not library:
        raise ProbeError("Set SEAFILE_BASE_URL, SEAFILE_LIBRARY_ID and SEAFILE_REPO_TOKEN.")
    allowed_origins = {https_origin(base)}
    if urlsplit(base).query:
        raise ProbeError("SEAFILE_BASE_URL must be the site base URL without a query.")
    for origin in args.transfer_origin:
        allowed_origins.add(https_origin(origin))
        if urlsplit(origin).path not in ("", "/") or urlsplit(origin).query:
            raise ProbeError("--transfer-origin must be an HTTPS origin without a path or query.")

    directory = args.directory.rstrip("/")
    if directory != "/接口测试":
        raise ProbeError("The deployment probe is limited to /接口测试.")
    auth = "Token" if args.legacy_token else "Bearer"

    def api(endpoint, path=None, *, method="GET", **params):
        url = f"{base}/api/v2.1/via-repo-token/{endpoint}/"
        if path is not None:
            query = urlencode({"path": path, **params})
            url += f"?{query}"
        headers = {"Authorization": f"{auth} {token}", "Accept": "application/json"}
        return parse_json(endpoint, request(endpoint, url, headers=headers, method=method))

    def list_names():
        entries = api("dir", directory)
        entries = entries.get("dirent_list") if isinstance(entries, dict) else None
        if not isinstance(entries, list):
            raise ProbeError("dir: expected a Seafile directory listing.")
        if not all(isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in entries):
            raise ProbeError("dir: invalid directory entries.")
        return {entry["name"] for entry in entries}

    def transfer_link(endpoint, path, **params):
        link = api(endpoint, path, **params)
        if not isinstance(link, str):
            raise ProbeError(f"{endpoint}: expected a temporary transfer URL.")
        if https_origin(link) not in allowed_origins:
            raise ProbeError(f"{endpoint}: check the file server origin before adding --transfer-origin.")
        return link

    info = api("repo-info")
    if not isinstance(info, dict) or info.get("repo_id") != library:
        raise ProbeError("Repository token does not match the configured library.")
    names = list_names()
    print("PASS read: authenticated directory listing.")
    if not args.roundtrip:
        return

    name = "teachhome-probe-" + secrets.token_hex(16) + ".txt"
    if name in names:
        raise ProbeError("Probe name collision. Re-run to generate a new name.")
    path = posixpath.join(directory, name)
    contents = ("teachhome Seafile test " + secrets.token_hex(32) + "\n").encode("ascii")
    digest = hashlib.sha256(contents).hexdigest()
    link = transfer_link("upload-link", directory, replace="0")
    headers, body = multipart(directory, name, contents)
    url = link + ("&" if urlsplit(link).query else "?") + "ret-json=1"
    # If networking fails after the POST, only an operator can confirm remote state.
    print(f"Probe file: {path} (check manually if interrupted).", flush=True)
    upload = parse_json("upload", request("upload", url, headers=headers, method="POST", data=body))
    if (not isinstance(upload, list) or len(upload) != 1 or not isinstance(upload[0], dict)
            or upload[0].get("name") != name):
        raise ProbeError("Upload response did not confirm the exact generated name. Check the test directory.")
    print("PASS upload: server confirmed the probe filename.")

    data = request("download", transfer_link("download-link", path), limit=len(contents))
    if hashlib.sha256(data).hexdigest() != digest:
        raise ProbeError("Download checksum mismatch. Inspect the probe before deleting it.")
    print(f"PASS download: SHA-256 {digest}.")
    # Delete only the random path whose contents matched this run's random payload.
    result = api("file", path, method="DELETE")
    deleted = isinstance(result, dict) and result.get("success") is True
    if not deleted or name in list_names():
        raise ProbeError("Could not confirm probe deletion. Check the test directory.")
    print("PASS cleanup: probe absent from the directory.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="/接口测试", help="dedicated probe directory (default /接口测试)")
    parser.add_argument("--roundtrip", action="store_true", help="upload, read back and delete a unique probe")
    parser.add_argument("--legacy-token", action="store_true", help="use Token authentication on servers before 11")
    parser.add_argument("--transfer-origin", action="append", default=[],
                        help="additional trusted HTTPS file server origin (repeatable)")
    args = parser.parse_args()
    try:
        run(args)
    except ProbeError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
