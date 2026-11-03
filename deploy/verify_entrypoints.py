"""Check the anonymous public entry points from outside the teaching host."""
import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class VerificationError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def get(url, origin=None, expected=200):
    headers = {"Origin": origin} if origin else {}
    request = Request(url, headers=headers)
    try:
        response = build_opener(NoRedirect()).open(request, timeout=15)
    except HTTPError as exc:
        response = exc
    with response:
        if response.status != expected:
            raise VerificationError(f"{urlsplit(url).path}: expected {expected}, got {response.status}")
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise VerificationError("Public response exceeded 4 MiB")
        return response.headers, body


def https_url(value, *, only_origin=False):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or "\\" in value
            or any(ord(c) < 33 for c in value) or (only_origin and parts.path not in ("", "/"))):
        raise VerificationError("Use an HTTPS address without credentials, a query or a fragment")
    return f"https://{parts.netloc}"


def check(portal, expected_origin):
    pages_origin = https_url(portal)
    portal = portal.rstrip("/") + "/"
    _, page = get(portal)
    if b'catalog.js' not in page:
        raise VerificationError("The Pages response is missing the catalog module")
    for asset in ("catalog.js", "portal.css"):
        _, body = get(urljoin(portal, asset))
        if not body:
            raise VerificationError(f"Empty portal asset: {asset}")
    _, raw_config = get(urljoin(portal, "config.json"))
    config = json.loads(raw_config)
    if not isinstance(config, dict) or "origin" not in config or not isinstance(config["origin"], str):
        raise VerificationError("Pages did not return a valid teaching-origin configuration")
    print("PASS public HTTPS portal, script, stylesheet and configuration")
    if not config["origin"]:
        print("BLOCKED Pages has no configured teaching origin")
        return 2
    if not expected_origin:
        print("BLOCKED provide --teaching-origin to confirm the intended host")
        return 2
    teaching = https_url(expected_origin, only_origin=True)
    if config["origin"] != teaching:
        raise VerificationError("Pages points to a different origin from the intended teaching host")
    _, raw_health = get(teaching + "/health/")
    if json.loads(raw_health) != {"status": "ok"}:
        raise VerificationError("Unexpected teaching health response")
    catalog = teaching + "/api/public/courses/"
    headers, body = get(catalog, origin=pages_origin)
    if (headers.get("Access-Control-Allow-Origin") != pages_origin
            or "origin" not in {v.strip().lower() for v in headers.get("Vary", "").split(",")}
            or headers.get("Access-Control-Allow-Credentials", "").lower() == "true"):
        raise VerificationError("Unexpected credentialless Pages CORS policy")
    other, _ = get(catalog, origin="https://field-check.invalid")
    if other.get("Access-Control-Allow-Origin"):
        raise VerificationError("The catalog granted CORS to an unrelated origin")
    payload = json.loads(body)
    courses = payload.get("courses") if isinstance(payload, dict) else None
    if not isinstance(courses, list) or any(not isinstance(c, dict) for c in courses):
        raise VerificationError("Unexpected public course index")
    if not courses:
        print("BLOCKED publish a synthetic course before verifying its anonymous chapter link")
        return 2
    chapter = next((s.get("url") for c in courses for s in c.get("chapters", [])
                    if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"]), None)
    if not chapter:
        print("BLOCKED the published catalog has no chapter to exercise")
        return 2
    chapter_url = urljoin(teaching + "/", chapter)
    if https_url(chapter_url) != teaching or not urlsplit(chapter_url).path.startswith("/courses/"):
        raise VerificationError("Catalog returned an unexpected public chapter URL")
    get(chapter_url)
    private, _ = get(teaching + "/me/", expected=302)
    login = urlsplit(urljoin(teaching, private.get("Location", "")))
    if (f"{login.scheme}://{login.netloc}" != teaching or login.path != "/login/"
            or "no-store" not in private.get("Cache-Control", "")):
        raise VerificationError("Anonymous personal page did not send a private login redirect")
    print("PASS teaching HTTPS, Pages CORS, published chapter and anonymous login boundary")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal", default="https://xiaoshecode.github.io/Racheltoolkit/")
    parser.add_argument("--teaching-origin", help="Expected teaching HTTPS origin, supplied by the operator")
    args = parser.parse_args()
    try:
        return check(args.portal, args.teaching_origin)
    except (VerificationError, URLError, OSError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
