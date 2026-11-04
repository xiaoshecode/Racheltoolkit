"""Publish one verified Git commit on the prepared Linux teaching host.

Run from the server checkout. Credentials remain in deploy/.env on that host.
A failed migration is deliberately left stopped; this script never restores a DB.
"""
import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from urllib.request import urlopen

REPOSITORY = "https://github.com/xiaoshecode/Racheltoolkit.git"


class ReleaseError(Exception):
    pass


def run(args, *, cwd, env=None, capture=False, stdin=None):
    result = subprocess.run(args, cwd=cwd, env=env, input=stdin, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        # Captured Compose/inspect output may contain credentials; never print it.
        raise ReleaseError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout.strip() if capture else ""


def health(origin, attempts=40):
    for attempt in range(attempts):
        try:
            with urlopen(origin + "/health/", timeout=10) as response:
                if response.url == origin + "/health/" and json.loads(response.read(1024)) == {"status": "ok"}:
                    return
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(3)
    raise ReleaseError("HTTPS health check failed; inspect the host privately")


def validate_configuration(config, root):
    service = config["services"]["web"]
    values = service.get("environment", {})
    if values.get("TEACH_DATA_DIR", "/data") != "/data" or values.get("TEACH_DB", "/data/school.sqlite3") != "/data/school.sqlite3":
        raise ReleaseError("Container data must remain /data with /data/school.sqlite3")
    host = values.get("TEACH_HOST", "")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host):
        raise ReleaseError("TEACH_HOST must be a DNS hostname")
    backup = Path(values.get("TEACH_BACKUP_DIR", ""))
    if not backup.is_absolute() or not backup.is_dir():
        raise ReleaseError("Set TEACH_BACKUP_DIR in deploy/.env to an existing private backup directory")
    backup = backup.resolve()
    if backup == Path("/") or backup == root or root in backup.parents:
        raise ReleaseError("Backup directory must be outside the source checkout")
    for volume in service.get("volumes", []):
        if volume.get("target") == "/data" and volume.get("type") == "bind":
            data = Path(volume["source"]).resolve()
            if data == backup or data in backup.parents or backup in data.parents:
                raise ReleaseError("Backup and teaching data must use separate directory trees")
    return host, backup


def verify_data_mount(config, previous):
    desired = next(v for v in config["services"]["web"]["volumes"] if v["target"] == "/data")
    existing = next(v for v in previous["Mounts"] if v["Destination"] == "/data")
    if desired["type"] == "volume":
        source = config["volumes"][desired["source"]]["name"]
        matches = existing["Type"] == "volume" and source == existing["Name"]
    else:
        matches = existing["Type"] == "bind" and Path(desired["source"]).resolve() == Path(existing["Source"]).resolve()
    old_env = dict(entry.split("=", 1) for entry in previous["Config"]["Env"])
    if (not matches or old_env.get("TEACH_DATA_DIR", "/data") != "/data"
            or old_env.get("TEACH_DB", "/data/school.sqlite3") != "/data/school.sqlite3"):
        raise ReleaseError("Data location changed; migrate and verify it manually before deploying")
    new_env = config["services"]["web"].get("environment", {})
    for key in ("SEAFILE_LIBRARY_ID", "SEAFILE_BASE_URL"):
        if old_env.get(key, "").rstrip("/") != new_env.get(key, "").rstrip("/"):
            raise ReleaseError("Private file library changed; migrate and verify it manually before deploying")


def persist_release(path, sha):
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not re.match(r"^\s*(?:export\s+)?TEACH_RELEASE\s*=", line)]
    temporary = path.with_name(".env.next")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(path.stat().st_mode))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("\n".join(lines + [f"TEACH_RELEASE={sha}"]) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(container, backup, stamp, *, root):
    # Read the running image/environment, so the pre-migration snapshot uses OLD code.
    info = json.loads(run(["docker", "inspect", container], cwd=root, capture=True))[0]
    values = info["Config"]["Env"]
    if any("\n" in entry or "\r" in entry for entry in values):
        raise ReleaseError("Container environment contains multiline values; use the documented manual backup")
    networks = info.get("NetworkSettings", {}).get("Networks", {})
    network = next(iter(networks), "bridge")
    run(["docker", "run", "--rm", "--network", network, "--volumes-from", container,
         "--mount", f"type=bind,src={backup},dst=/backups",
         "--env-file", "/dev/stdin", info["Image"], "python", "app/manage.py",
         "snapshot_teaching", f"/backups/{stamp}"],
        cwd=root, stdin="\n".join(values) + "\n")
    run(["docker", "run", "--rm", "--network", "none", "--mount",
         f"type=bind,src={backup},dst=/backups,readonly", info["Image"], "python", "-c",
         f"from pathlib import Path; assert Path('/backups/{stamp}/manifest.json').is_file()"], cwd=root)
    print(f"PASS pre-migration snapshot: {backup / stamp}", flush=True)


def deploy(root, sha):
    import fcntl  # Linux host only; no locks or daemon access on the Windows workstation.
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("Supply the complete lowercase 40-character commit SHA")
    root = root.resolve()
    if run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture=True) != str(root):
        raise ReleaseError("Run from the root of the prepared server checkout")
    runtime = root / ".local"
    runtime.mkdir(exist_ok=True)
    marker = runtime / "deploy-incomplete"
    with (runtime / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError("Another deployment, preview or backup is running; retry later") from exc
        if marker.exists():
            raise ReleaseError("Previous deployment needs inspection: .local/deploy-incomplete")
        if run(["git", "status", "--porcelain"], cwd=root, capture=True):
            raise ReleaseError("Server checkout has local changes; preserve and resolve them before deployment")
        if not (root / "deploy/.env").is_file():
            raise ReleaseError("Prepare private deploy/.env on the server first")
        previous = run(["git", "rev-parse", "HEAD"], cwd=root, capture=True)
        run(["git", "fetch", "--no-tags", REPOSITORY, "main"], cwd=root)
        run(["git", "merge-base", "--is-ancestor", sha, "FETCH_HEAD"], cwd=root)
        env = dict(os.environ, TEACH_RELEASE=sha)
        compose = ["docker", "compose", "--env-file", ".env"]
        working = root / "deploy"
        old_container = ""
        stopped = False
        migration_started = False
        marker.write_text(f"requested={sha}\nprevious={previous}\n", encoding="utf-8")
        try:
            run(["git", "checkout", "--detach", sha], cwd=root)
            config = json.loads(run(compose + ["config", "--format", "json"], cwd=working, env=env, capture=True))
            host, backup = validate_configuration(config, root)
            old_container = run(compose + ["ps", "-aq", "web"], cwd=working, env=env, capture=True)
            if "\n" in old_container:
                raise ReleaseError("Expected one teaching web container")
            if old_container:
                previous_container = json.loads(run(["docker", "inspect", old_container], cwd=root, capture=True))[0]
                verify_data_mount(config, previous_container)
            run(compose + ["build", "web"], cwd=working, env=env)
            run(compose + ["run", "--rm", "--no-deps", "web", "python", "app/manage.py",
                           "check", "--deploy", "--fail-level", "WARNING"], cwd=working, env=env)
            if not old_container:
                exists = run(compose + ["run", "--rm", "--no-deps", "web", "python", "-c",
                             "from pathlib import Path; print(int(Path('/data/school.sqlite3').exists()))"],
                             cwd=working, env=env, capture=True)
                if exists != "0":
                    raise ReleaseError("Existing database has no prior container; take a verified manual backup first")
            if old_container:
                run(compose + ["stop", "-t", "45", "web"], cwd=working, env=env)
                stopped = True
                stamp = datetime.datetime.now(datetime.timezone.utc).strftime("release-%Y%m%dT%H%M%S.%fZ")
                snapshot(old_container, backup, stamp, root=root)
            # From here, an automatic checkout/database rollback would be unsafe.
            migration_started = True
            run(compose + ["run", "--rm", "--no-deps", "web", "python", "app/manage.py",
                           "migrate", "--noinput"], cwd=working, env=env)
            worker_python = root / ".venv/bin/python"
            if worker_python.is_file():
                worker_env = dict(env, PIP_CACHE_DIR=str(runtime / "cache"), TMPDIR=str(runtime))
                run([str(worker_python), "-m", "pip", "install", "-r", "requirements.txt"],
                    cwd=root, env=worker_env)
            run(compose + ["up", "-d", "web", "https"], cwd=working, env=env)
            health("https://" + host)
            persist_release(working / ".env", sha)
            if shutil.which("systemctl"):
                for timer in ("racheltoolkit-previews.timer", "racheltoolkit-backup.timer"):
                    enabled = subprocess.run(["systemctl", "is-enabled", timer], capture_output=True)
                    if enabled.returncode == 0:
                        run(["systemctl", "restart", timer], cwd=root)
            (runtime / "current-release").write_text(sha + "\n", encoding="utf-8")
            marker.unlink()
            print(f"PASS web release {sha}: https://{host}/", flush=True)
            print("Run the documented Seafile and public-entrypoint checks before field acceptance.")
        except BaseException:
            if not migration_started:
                run(["git", "checkout", "--detach", previous], cwd=root)
                if stopped and old_container:
                    run(["docker", "start", old_container], cwd=root)
                marker.unlink()
            else:
                # Avoid serving a partly migrated database or letting timers modify it.
                with contextlib.suppress(Exception):
                    run(compose + ["stop", "-t", "45", "web"], cwd=working, env=env)
                print("Deployment stopped after migration began. The database was NOT restored. "
                      "Inspect .local/deploy-incomplete and follow deploy/README.md.", file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha")
    args = parser.parse_args()
    try:
        deploy(Path.cwd(), args.sha)
    except (ReleaseError, OSError, ValueError, KeyError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
