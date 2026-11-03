import json
import os
from pathlib import Path
from urllib.parse import urlsplit

origin = os.environ.get("TEACH_ORIGIN", "").strip().rstrip("/")
parts = urlsplit(origin)
if origin and (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
               or parts.path or parts.query or parts.fragment):
    raise ValueError("TEACH_ORIGIN must be the verified HTTPS origin of the teaching host")
portal = Path("portal")
expected = {"index.html", "portal.css", "catalog.js", "config.json"}
if {p.name for p in portal.iterdir()} != expected or any(not p.is_file() or p.is_symlink() for p in portal.iterdir()):
    raise ValueError("Review unexpected portal contents before publishing")
(portal / "config.json").write_text(json.dumps({"origin": origin}), encoding="utf-8")

