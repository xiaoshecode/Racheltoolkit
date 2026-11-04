"""Start an on-demand Codespaces trial; never provisions or keeps a VM alive."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import venv
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".local/codespaces"
PYTHON = ROOT / ".venv-codespaces/bin/python"
IMAGE = "racheltoolkit-office:codespaces"


def environment():
    if os.environ.get("CODESPACES") != "true" or sys.platform != "linux":
        raise RuntimeError("Run this command inside a GitHub Codespace.")
    name = os.environ.get("CODESPACE_NAME", "")
    domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or domain != "app.github.dev":
        raise RuntimeError("Missing or invalid GitHub Codespaces forwarding identity.")
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.chmod(0o700)
    # The web/worker processes do not inherit the Codespace's GitHub credentials.
    allowed = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "DOCKER_HOST", "XDG_RUNTIME_DIR")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    secret_path = DATA / "secret.key"
    if not secret_path.exists():
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(secrets.token_hex(48))
    env.update(CODESPACES="true", CODESPACE_NAME=name,
               GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN=domain,
               TEACH_HOST=f"{name}-8000.{domain}", DJANGO_SETTINGS_MODULE="config.codespaces",
               DJANGO_SECRET_KEY=secret_path.read_text().strip(), TEACH_DATA_DIR=str(DATA),
               TEACH_DB=str(DATA / "school.sqlite3"), TEACH_STORAGE_BACKEND="local",
               PYTHONPATH=str(ROOT / "app"), PYTHONUNBUFFERED="1",
               PIP_CACHE_DIR=str(ROOT / ".local/cache"), PIP_DISABLE_PIP_VERSION_CHECK="1",
               TMPDIR=str(DATA), TEMP=str(DATA), TMP=str(DATA))
    if os.environ.get("TEACH_STORAGE_BACKEND") == "seafile":
        env["TEACH_STORAGE_BACKEND"] = "seafile"
        for key in ("SEAFILE_BASE_URL", "SEAFILE_LIBRARY_ID", "SEAFILE_REPO_TOKEN", "SEAFILE_TRANSFER_ORIGINS"):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def run(args, env, **kwargs):
    return subprocess.run([str(arg) for arg in args], cwd=ROOT, env=env, check=True, **kwargs)


def manage(env, *args, **kwargs):
    return run([PYTHON, ROOT / "app/manage.py", *args], env, **kwargs)


def backup(env, label):
    parent = DATA / "backups"
    parent.mkdir(exist_ok=True, mode=0o700)
    destination = parent / (time.strftime(label + "-%Y%m%d-%H%M%S-") + secrets.token_hex(3))
    manage(env, "snapshot_teaching", str(destination))
    return destination


def healthy(env):
    try:
        request = Request("http://127.0.0.1:8000/health/", headers={"Host": env["TEACH_HOST"]})
        with urlopen(request, timeout=2) as response:
            return response.status == 200 and json.loads(response.read(1024)) == {"status": "ok"}
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def exclusive():
    import fcntl
    with (DATA / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("The teaching service is already running. Stop it before setup or migration.") from None
        yield


def setup(env):
    with exclusive():
        if sys.version_info[:2] != (3, 13):
            raise RuntimeError("Use Python 3.13 from the configured dev container.")
        if not PYTHON.exists():
            venv.EnvBuilder(with_pip=True).create(PYTHON.parents[1])
        run([PYTHON, "-m", "pip", "install", "-r", ROOT / "requirements.txt"], env)
        run(["docker", "info"], env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run(["docker", "build", "-t", IMAGE, ROOT / "app/storage/office"], env)
        image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE], env,
                       capture_output=True, text=True).stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise RuntimeError("Office image did not return an immutable identifier.")
        env["TEACH_OFFICE_IMAGE"] = image_id
        manage(env, "check", "--deploy", "--fail-level", "WARNING")
        if (DATA / "school.sqlite3").exists():
            # Preserve an application-consistent recovery copy before migrations.
            backup(env, "upgrade")
        manage(env, "migrate", "--noinput")
        manage(env, "collectstatic", "--noinput")
        manage(env, "bootstrap_codespace")
        (DATA / "runtime.json").write_text(json.dumps({"office_image": image_id}), encoding="utf-8")
        print("Trial setup ready. Private administrator card: .local/codespaces/access.txt")


def serve(env):
    with exclusive():
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        record = json.loads((DATA / "runtime.json").read_text())
        image_id = record["office_image"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise RuntimeError("Run setup to prepare the Office renderer.")
        run(["docker", "image", "inspect", image_id], env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env["TEACH_OFFICE_IMAGE"] = image_id
        manage(env, "migrate", "--check")
        with (DATA / "web.log").open("a") as web_log, (DATA / "previews.log").open("a") as worker_log:
            web = subprocess.Popen([str(PYTHON), "-m", "waitress", "--listen=0.0.0.0:8000",
                                    "--url-scheme=https", "--threads=8", "--max-request-body-size=110100480",
                                    "config.codespaces_wsgi:application"],
                                   cwd=ROOT, env=env, stdout=web_log, stderr=web_log)
            try:
                (DATA / "server.pid").write_text(str(os.getpid()))
                while web.poll() is None and not stopping.is_set():
                    try:
                        manage(env, "render_previews", "--limit", "5", stdout=worker_log, stderr=worker_log)
                    except subprocess.CalledProcessError:
                        pass  # A failed preview retains its original and can be retried in the UI.
                    stopping.wait(20)
                if not stopping.is_set():
                    raise RuntimeError("Web service exited; inspect .local/codespaces/web.log.")
            finally:
                if web.poll() is None:
                    web.terminate()
                    try:
                        web.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        web.kill()
                        web.wait()
                (DATA / "server.pid").unlink(missing_ok=True)


def stop():
    pid_file = DATA / "server.pid"
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text())
    if pid <= 1:
        raise RuntimeError("Invalid service PID; inspect the Codespace privately.")
    command = Path(f"/proc/{pid}/cmdline")
    if not command.exists():
        return
    arguments = command.read_bytes().decode().split("\0")
    if str(Path(__file__).resolve()) not in arguments or "serve" not in arguments:
        raise RuntimeError("PID belongs to a different process; it was not stopped.")
    os.kill(pid, signal.SIGTERM)
    for _ in range(120):
        if not pid_file.exists() or not command.exists():
            return
        time.sleep(1)
    raise RuntimeError("Service is finishing a preview. Retry stop after it completes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "start", "serve", "stop", "backup"))
    args = parser.parse_args()
    env = environment()
    if args.action == "setup":
        setup(env)
    elif args.action == "serve":
        serve(env)
    elif args.action == "stop":
        stop()
    elif args.action == "backup":
        destination = backup(env, "manual")
        print(f"Backup ready at {destination}. Download it privately before deleting the Codespace.")
    else:
        if not (DATA / "runtime.json").exists():
            raise RuntimeError("Run python3 scripts/codespaces.py setup first.")
        with (DATA / "service.log").open("a") as log:
            process = subprocess.Popen([str(PYTHON), __file__, "serve"], cwd=ROOT, env=env,
                                       stdout=log, stderr=log, start_new_session=True)
        pid_file = DATA / "server.pid"
        ready = False
        for _ in range(20):
            if process.poll() is not None:
                break
            if pid_file.exists() and pid_file.read_text() == str(process.pid) and healthy(env):
                ready = True
                break
            time.sleep(1)
        if not ready or process.poll() is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("Startup failed; the owned service is stopping after a preview. Inspect service.log before retrying.") from None
            raise RuntimeError("Service did not start; inspect .local/codespaces/service.log.")
        print(f"Teaching trial: https://{env['TEACH_HOST']}")
        print("Port 8000 remains private until you explicitly share it in the Codespaces Ports panel.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        # Never include subprocess output or environment values in this error path.
        print(f"Codespaces startup failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
