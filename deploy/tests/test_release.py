import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("teaching_release", ROOT / "deploy/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = "a" * 40
OLD = "b" * 40


class Fixture:
    def setUp(self):
        (ROOT / ".local").mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / ".local")
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.backup = self.base / "backups"
        self.backup.mkdir()
        self.config = {
            "services": {"web": {
                "environment": {"TEACH_HOST": "teaching.example.org", "TEACH_BACKUP_DIR": str(self.backup)},
                "volumes": [{"type": "bind", "source": str(self.base / "data"), "target": "/data"}],
            }},
        }


class ConfigurationTests(Fixture, unittest.TestCase):
    def test_backups_cannot_be_nested_in_live_data_or_source(self):
        release.validate_configuration(self.config, self.root)
        self.config["services"]["web"]["environment"]["TEACH_BACKUP_DIR"] = str(self.root)
        with self.assertRaises(release.ReleaseError):
            release.validate_configuration(self.config, self.root)
        self.config["services"]["web"]["environment"]["TEACH_BACKUP_DIR"] = str(self.backup)
        self.config["services"]["web"]["volumes"][0]["source"] = str(self.base)
        with self.assertRaises(release.ReleaseError):
            release.validate_configuration(self.config, self.root)

    def test_changed_data_mount_and_database_are_rejected(self):
        old = {"Mounts": [{"Type": "bind", "Destination": "/data", "Source": str(self.base / "data")}],
               "Config": {"Env": ["TEACH_DB=/data/school.sqlite3"]}}
        release.verify_data_mount(self.config, old)
        old["Mounts"][0]["Source"] = str(self.base / "different-data")
        with self.assertRaises(release.ReleaseError):
            release.verify_data_mount(self.config, old)
        old["Mounts"][0]["Source"] = str(self.base / "data")
        old["Config"]["Env"] = ["TEACH_DB=/data/other.sqlite3"]
        with self.assertRaises(release.ReleaseError):
            release.verify_data_mount(self.config, old)
        old["Config"]["Env"] = ["TEACH_DATA_DIR=/data/other"]
        with self.assertRaises(release.ReleaseError):
            release.verify_data_mount(self.config, old)

    def test_named_volume_identity_must_match(self):
        self.config["services"]["web"]["volumes"][0] = {"type": "volume", "source": "teaching-data", "target": "/data"}
        self.config["volumes"] = {"teaching-data": {"name": "deploy_teaching-data"}}
        old = {"Mounts": [{"Type": "volume", "Destination": "/data", "Name": "deploy_teaching-data"}],
               "Config": {"Env": []}}
        release.verify_data_mount(self.config, old)
        old["Mounts"][0]["Name"] = "different_teaching-data"
        with self.assertRaises(release.ReleaseError):
            release.verify_data_mount(self.config, old)

    def test_library_change_is_rejected_but_token_rotation_is_allowed(self):
        old = {"Mounts": [{"Type": "bind", "Destination": "/data", "Source": str(self.base / "data")}],
               "Config": {"Env": ["SEAFILE_LIBRARY_ID=original", "SEAFILE_BASE_URL=https://storage.example.org", "SEAFILE_REPO_TOKEN=old"]}}
        values = self.config["services"]["web"]["environment"]
        values.update(SEAFILE_LIBRARY_ID="original", SEAFILE_BASE_URL="https://storage.example.org", SEAFILE_REPO_TOKEN="rotated")
        release.verify_data_mount(self.config, old)
        values["SEAFILE_LIBRARY_ID"] = "different"
        with self.assertRaises(release.ReleaseError):
            release.verify_data_mount(self.config, old)

    def test_release_persistence_preserves_credentials_and_drops_old_duplicates(self):
        path = self.root / ".env"
        original_secret = "synthetic-secret-for-unit-test"
        path.write_text(f"# configuration\nTEACH_RELEASE=old\nDJANGO_SECRET_KEY={original_secret}\n export TEACH_RELEASE=older\n", encoding="utf-8")
        release.persist_release(path, SHA)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         f"# configuration\nDJANGO_SECRET_KEY={original_secret}\nTEACH_RELEASE={SHA}\n")
        self.assertFalse(path.with_name(".env.next").exists())


@unittest.skipUnless(sys.platform == "linux", "Real flock is tested on Linux CI")
class UpgradeFailureTests(Fixture, unittest.TestCase):
    def test_running_maintenance_blocks_release_before_fetch_or_checkout(self):
        import fcntl
        runtime = self.root / ".local"
        runtime.mkdir()
        with (runtime / "deploy.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(release, "run", return_value=str(self.root)) as command:
                with self.assertRaisesRegex(release.ReleaseError, "Another deployment"):
                    release.deploy(self.root, SHA)
                self.assertEqual(command.call_count, 1)
        self.assertFalse((runtime / "deploy-incomplete").exists())

    def simulate(self, *, failed_stage=None, has_container=True, existing_database=False):
        working = self.root / "deploy"
        working.mkdir()
        (working / ".env").write_text("TEACH_RELEASE=old\n", encoding="utf-8")
        calls = []
        old = {"Mounts": [{"Type": "bind", "Destination": "/data", "Source": str(self.base / "data")}],
               "Config": {"Env": []}}

        def invoke(args, **kwargs):
            calls.append(args)
            if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return str(self.root)
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return OLD
            if args[:2] == ["docker", "inspect"]:
                return json.dumps([old])
            if "config" in args:
                return json.dumps(self.config)
            if "ps" in args:
                return "previous-container" if has_container else ""
            if "-c" in args and "python" in args:
                return "1" if existing_database else "0"
            if "migrate" in args and failed_stage == "migration":
                raise release.ReleaseError("injected migration failure")
            return ""

        def backup(*args, **kwargs):
            calls.append(["snapshot"])
            if failed_stage == "backup":
                raise release.ReleaseError("injected storage failure")

        def healthy(*args):
            calls.append(["health"])
            if failed_stage == "health":
                raise release.ReleaseError("injected HTTPS failure")

        with patch.object(release, "run", side_effect=invoke), patch.object(release, "snapshot", side_effect=backup), patch.object(release, "health", side_effect=healthy):
            if failed_stage or (existing_database and not has_container):
                with self.assertRaises(release.ReleaseError):
                    release.deploy(self.root, SHA)
            else:
                release.deploy(self.root, SHA)
        return calls

    def test_backup_failure_restarts_previous_image_without_running_migrations(self):
        calls = self.simulate(failed_stage="backup")
        self.assertIn(["docker", "start", "previous-container"], calls)
        self.assertIn(["git", "checkout", "--detach", OLD], calls)
        self.assertFalse(any("migrate" in call for call in calls))
        self.assertFalse((self.root / ".local/deploy-incomplete").exists())

    def test_migration_failure_holds_maintenance_without_restoring_database(self):
        calls = self.simulate(failed_stage="migration")
        self.assertIn(["snapshot"], calls)
        self.assertNotIn(["git", "checkout", "--detach", OLD], calls)
        self.assertNotIn(["docker", "start", "previous-container"], calls)
        self.assertTrue((self.root / ".local/deploy-incomplete").exists())
        self.assertFalse((self.root / ".local/current-release").exists())

    def test_unhealthy_new_release_stays_stopped_and_does_not_change_persisted_release(self):
        calls = self.simulate(failed_stage="health")
        self.assertIn(["health"], calls)
        self.assertEqual(calls[-1][-4:], ["stop", "-t", "45", "web"])
        self.assertTrue((self.root / ".local/deploy-incomplete").exists())
        self.assertEqual((self.root / "deploy/.env").read_text(encoding="utf-8"), "TEACH_RELEASE=old\n")

    def test_existing_database_without_old_container_cannot_bypass_snapshot(self):
        calls = self.simulate(has_container=False, existing_database=True)
        self.assertFalse(any("migrate" in call for call in calls))
        self.assertNotIn(["snapshot"], calls)

    def test_healthy_upgrade_snapshots_before_migration_and_persists_image(self):
        calls = self.simulate()
        migration = next(i for i, call in enumerate(calls) if "migrate" in call)
        self.assertLess(calls.index(["snapshot"]), migration)
        self.assertEqual((self.root / ".local/current-release").read_text(encoding="utf-8").strip(), SHA)
        self.assertIn(f"TEACH_RELEASE={SHA}", (self.root / "deploy/.env").read_text(encoding="utf-8"))
        self.assertFalse((self.root / ".local/deploy-incomplete").exists())


if __name__ == "__main__":
    unittest.main()
