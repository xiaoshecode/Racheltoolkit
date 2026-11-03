"""Create a consistent, independently restorable private snapshot in a NEW directory."""

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from storage.backends import get_backend, library_id, validate_path


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def inventory(db):
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as snap:
        if (
            snap.execute("PRAGMA integrity_check").fetchone() != ("ok",)
            or snap.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise CommandError("Snapshot database failed integrity checks")
        snap.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in snap.execute(
                "SELECT id, path, size, sha256, library_id, state FROM storage_storedfile ORDER BY id"
            )
        ]


def failed_only_files(db):
    """Skip a quarantined original only when the snapshot proves it belongs solely to a failed attempt."""
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as snap:
        rows = snap.execute("""
            SELECT a.file_id FROM submissions_submissionattachment a
            JOIN submissions_submission s ON s.id = a.submission_id
            WHERE s.state = 'quarantined'
            EXCEPT SELECT source_file_id FROM storage_previewasset
            EXCEPT SELECT output_file_id FROM storage_previewasset
            EXCEPT SELECT original_file_id FROM catalog_material
            EXCEPT SELECT preview_file_id FROM catalog_material
            EXCEPT SELECT file_id FROM grading_answerkeyrevision
        """)
        return {row[0] for row in rows}


class Command(BaseCommand):
    help = "Back up SQLite and the exact immutable file versions referenced by the snapshot."

    def add_arguments(self, parser):
        parser.add_argument("destination", type=Path)

    def handle(self, destination, **options):
        target = destination.resolve()
        if target.exists() or not target.parent.is_dir():
            raise CommandError("Choose a new destination in an existing private backup directory")
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=target.parent) as tmp:
            bundle = Path(tmp) / "bundle"
            bundle.mkdir(mode=0o700)
            db = bundle / "school.sqlite3"
            connection.ensure_connection()
            with closing(sqlite3.connect(db)) as snap:
                connection.connection.backup(snap)
            rows = inventory(db)
            backend = get_backend()
            count = 0
            skipped = 0
            optional = (
                failed_only_files(db)
                if any(row["state"] == "quarantined" for row in rows)
                else set()
            )
            for row in rows:
                if row["state"] == "quarantined":
                    if row["id"] not in optional:
                        raise CommandError(
                            "Quarantined file is referenced outside a failed intake; resolve before signing a backup"
                        )
                    row["copied"] = False
                    skipped += 1
                    continue
                validate_path(row["path"])
                if row["library_id"] != library_id():
                    raise CommandError("Snapshot references a different library")
                source = backend.open(row["path"])
                if source is None:
                    if row["state"] == "ready":
                        raise CommandError("Confirmed file is missing")
                    row["copied"] = False
                    continue
                path = bundle / "objects" / row["path"].lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                with source, path.open("xb") as out:
                    shutil.copyfileobj(source, out)
                if path.stat().st_size != row["size"] or digest(path) != row["sha256"]:
                    raise CommandError("Remote file differs from snapshot metadata")
                row["copied"] = True
                count += 1
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {"database_sha256": digest(db), "objects": rows}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            bundle.rename(target)
        self.stdout.write(
            f"Snapshot complete: indexed={len(rows)} copied={count} quarantined_failed_attempts={skipped}"
        )
