import json
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from storage.backends import validate_path
from .snapshot_teaching import digest, inventory

class Command(BaseCommand):
    help = "Validate a private snapshot and materialize an isolated local teaching data directory."
    def add_arguments(self, parser):
        parser.add_argument("snapshot", type=Path)
        parser.add_argument("destination", type=Path)

    def handle(self, snapshot, destination, **options):
        start = snapshot.resolve()
        target = destination.resolve()
        if (target.exists() or target.is_relative_to(start) or
                target.is_relative_to(settings.RUNTIME.resolve()) or not target.parent.is_dir()):
            raise CommandError("Restore only into a NEW isolated directory outside the live data and backup")
        db = start / "school.sqlite3"
        manifest = json.loads((start / "manifest.json").read_text(encoding="utf-8"))
        if digest(db) != manifest["database_sha256"]:
            raise CommandError("Database checksum mismatch")
        rows = inventory(db)
        libraries = {row["library_id"] for row in rows}
        if len(libraries) > 1:
            raise CommandError("This installation requires a single private teaching library")
        if rows != [{k:v for k,v in row.items() if k != "copied"} for row in manifest["objects"]]:
            raise CommandError("Manifest is inconsistent with database inventory")
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=target.parent) as tmp:
            restored = Path(tmp) / "restored"
            restored.mkdir(mode=0o700)
            shutil.copyfile(db, restored / "school.sqlite3")
            if libraries:
                (restored / "restored-library.txt").write_text(libraries.pop(), encoding="utf-8")
            count = 0
            for row in manifest["objects"]:
                validate_path(row["path"])
                if not row["copied"]:
                    if row["state"] == "ready":
                        raise CommandError("Missing required object")
                    continue
                suffix = row["path"].lstrip("/")
                src = start / "objects" / suffix
                dest = restored / "private/storage" / suffix
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                if dest.stat().st_size != row["size"] or digest(dest) != row["sha256"]:
                    raise CommandError("Restored file checksum mismatch")
                count += 1
            restored.rename(target)
        self.stdout.write(f"Isolated restore complete: indexed={len(rows)} verified={count}")

