"""Bounded, repository-scoped local development commands (Python stdlib only)."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"
ENV = ROOT / ".env"
SERVICES = ("db-dev", "db-test")
VERSIONS = {"node": "v24.21.0", "npm": "11.19.0", "uv": "0.11.19"}
TOOLS = {name: os.environ.get(f"B71_{name.upper()}", name) for name in ("node", "npm", "uv", "docker")}


class Failure(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def run(command, *, cwd=ROOT, capture=False, timeout=120, env=None):
    try:
        result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture,
                                timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f"Cannot run {command[0]}: {exc}", 2) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise Failure(f"{command[0]} failed ({result.returncode}){': ' + detail if detail else ''}", 3)
    return result.stdout.strip() if capture else ""


def tool(name, *args):
    return [TOOLS[name], *args]


def compose(*args, env=None, capture=False, timeout=120):
    try:
        return run(tool("docker", "compose", *args), env=env, capture=capture, timeout=timeout)
    except Failure as exc:
        raise Failure(f"docker compose {args[0]} failed (exit {exc.code}); check Docker and project service status", exc.code) from exc


def preflight(*, docker=False, frontend=False, backend=False):
    if sys.version_info[:3] != (3, 13, 15):
        raise Failure(f"Python 3.13.15 required; found {sys.version.split()[0]}", 2)
    if frontend:
        for name in ("node", "npm"):
            try:
                actual = run(tool(name, "--version"), capture=True, env=command_env())
            except Failure as exc:
                raise Failure(f"{name} is unavailable: {exc}", 2) from exc
            if actual != VERSIONS[name]:
                raise Failure(f"{name} {VERSIONS[name]} required; found {actual}", 2)
    if backend:
        try:
            actual = run(tool("uv", "--version"), capture=True).split()
        except Failure as exc:
            raise Failure(f"uv is unavailable: {exc}", 2) from exc
        if len(actual) < 2 or actual[1] != VERSIONS["uv"]:
            raise Failure(f"uv {VERSIONS['uv']} required; found {' '.join(actual)}", 2)
    if docker:
        try:
            run(tool("docker", "compose", "version"), capture=True)
            run(tool("docker", "info", "--format", "{{.ServerVersion}}"), capture=True, timeout=15)
        except Failure as exc:
            raise Failure(f"Docker Compose and a running daemon are required: {exc}", 2) from exc


def command_env():
    result = os.environ.copy()
    # npm is a script on some platforms; ensure an overridden Node is found by it.
    node = Path(TOOLS["node"])
    if node.is_absolute():
        result["PATH"] = str(node.parent) + os.pathsep + result.get("PATH", "")
    return result


def setup():
    preflight(docker=True, frontend=True, backend=True)
    if not ENV.exists():
        try:
            descriptor = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((ROOT / ".env.example").read_bytes())
            print("Created .env from .env.example")
    run(tool("npm", "ci"), cwd=FRONTEND, timeout=600, env=command_env())
    run(tool("uv", "sync", "--locked", "--no-managed-python", "--python", sys.executable),
        cwd=BACKEND, timeout=600)
    compose("up", "-d", *SERVICES, timeout=300)
    wait_databases()
    api_probe()
    checks()


def wait_databases():
    for service in SERVICES:
        for attempt in range(30):
            try:
                status = json.loads(compose("ps", "--format", "json", service, capture=True))
                records = status if isinstance(status, list) else [status]
                if any(record.get("Health") == "healthy" and
                       record.get("Service", service) == service for record in records):
                    break
            except (Failure, ValueError, AttributeError):
                pass
            time.sleep(2)
        else:
            raise Failure(f"{service} did not become healthy within 60 seconds", 4)
        # psql returns a nonzero status on SQL/connection errors. Output has no credentials.
        result = compose("exec", "-T", service, "sh", "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT 1"',
                capture=True)
        if result != "1":
            raise Failure(f"{service}: SELECT 1 returned an unexpected result", 4)
        print(f"{service}: SELECT 1 passed")


def api_probe():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    child = spawn(tool("uv", "run", "--no-sync", "--env-file", str(ENV), "python",
                       "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                       "--port", str(port)), cwd=BACKEND)
    try:
        for _ in range(30):
            if child.poll() is not None:
                raise Failure("API exited before readiness", 4)
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/ready", timeout=2) as response:
                    if response.status == 200 and response.read() == b'{"status":"ready"}':
                        print("API readiness passed")
                        return
            except (URLError, HTTPError, TimeoutError):
                pass
            time.sleep(1)
        raise Failure("API readiness failed within 30 seconds", 4)
    finally:
        stop_child(child)


def checks():
    preflight(frontend=True, backend=True)
    run(tool("npm", "run", "typecheck"), cwd=FRONTEND, timeout=120, env=command_env())
    run(tool("uv", "run", "--no-sync", "python", "-m", "unittest", "discover", "-s", "tests", "-v"),
        cwd=BACKEND, timeout=120)
    compose("config", capture=True)


def build():
    preflight(frontend=True)
    run(tool("npm", "run", "build"), cwd=FRONTEND, timeout=180, env=command_env())


def spawn(command, cwd):
    kwargs = {"cwd": cwd, "env": command_env()}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def stop_child(child):
    if os.name == "nt":
        if child.poll() is None:
            child.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], check=False)
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=5)


def dev():
    preflight(docker=True, frontend=True, backend=True)
    if not ENV.exists() or not (BACKEND / ".venv").exists() or not (FRONTEND / "node_modules").exists():
        raise Failure("Run setup first", 2)
    compose("up", "-d", *SERVICES, timeout=300)
    wait_databases()
    children = []
    try:
        children.append(spawn(tool("uv", "run", "--no-sync", "--env-file", str(ENV), "python",
                                   "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                   "--port", "8000", "--reload"), BACKEND))
        children.append(spawn(tool("npm", "run", "dev"), FRONTEND))
        print("API: http://127.0.0.1:8000/docs | UI: http://127.0.0.1:5173", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.25)
        raise Failure("A development process exited", 4)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            stop_child(child)


def stop():
    preflight(docker=True)
    compose("stop", *SERVICES)


def clean():
    preflight(frontend=True, backend=True)
    run(tool("npm", "run", "clean"), cwd=FRONTEND, env=command_env())
    run([sys.executable, "scripts/clean_cache.py"], cwd=BACKEND)
    shutil.rmtree(ROOT / "scripts" / "__pycache__", ignore_errors=True)


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def main():
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "dev", "check", "build", "stop", "clean"))
    args = parser.parse_args()
    try:
        {"setup": setup, "dev": dev, "check": checks, "build": build,
         "stop": stop, "clean": clean}[args.command]()
    except Failure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
