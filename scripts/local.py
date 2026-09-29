"""Bounded, repository-scoped local development commands (Python stdlib only)."""

import argparse
import ctypes
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
sys.path.insert(0, str(BACKEND / "tests"))
from db_guard import UnsafeTestDatabase, guarded_url, read_settings  # noqa: E402

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
    executable = TOOLS[name]
    if os.name == "nt":
        executable = shutil.which(executable) or executable
    return [executable, *args]


def compose(*args, env=None, capture=False, timeout=120, test=False):
    options = ("--project-directory", str(ROOT), "--env-file", str(ENV),
               "-f", str(ROOT / "compose.yaml"), "-p", "b7-1") if test else ()
    try:
        return run(tool("docker", "compose", *options, *args), env=env,
                   capture=capture, timeout=timeout)
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
    setup_dependencies()
    compose("up", "-d", *SERVICES, timeout=300)
    wait_databases()
    api_probe()
    checks()


def setup_dependencies():
    """Install locked local dependencies without changing service state."""
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
    browser_env = command_env()
    browser_env["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".cache" / "playwright")
    run(tool("npm", "exec", "--", "playwright", "install", "chromium"),
        cwd=FRONTEND, timeout=600, env=browser_env)


def wait_databases(services=SERVICES, *, test=False):
    for service in services:
        for attempt in range(30):
            try:
                status = json.loads(compose("ps", "--format", "json", service,
                                            capture=True, test=test))
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
                capture=True, test=test)
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
    run(tool("uv", "run", "--no-sync", "pytest", "-m", "not db", "-q"),
        cwd=BACKEND, timeout=120)
    compose("config", capture=True)


def quality():
    preflight(frontend=True, backend=True)
    for task in ("format:check", "lint", "typecheck"):
        run(tool("npm", "run", task), cwd=FRONTEND, timeout=120, env=command_env())
    for args in (("ruff", "format", "--check", "app", "tests", "scripts"),
                 ("ruff", "check", "app", "tests", "scripts"), ("mypy", "app")):
        run(tool("uv", "run", "--no-sync", *args), cwd=BACKEND, timeout=120)


def build():
    preflight(frontend=True)
    run(tool("npm", "run", "build"), cwd=FRONTEND, timeout=180, env=command_env())


class WindowsJob:
    """Keep a launcher and all descendants owned even after the launcher exits."""

    def __init__(self, pid):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        process = None
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process:
                raise OSError(ctypes.get_last_error(), "OpenProcess failed")
            if not kernel.AssignProcessToJobObject(self.handle, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def spawn(command, cwd, *, env=None):
    kwargs = {"cwd": cwd, "env": env if env is not None else command_env()}
    if os.name == "nt":
        # The gate prevents the launcher from creating descendants until the job owns it.
        bridge = ("import json,subprocess,sys; "
                  "sys.exit(subprocess.call(json.loads(sys.argv[1]), cwd=sys.argv[2]) "
                  "if sys.stdin.buffer.read(1) == b'1' else 1)")
        child = subprocess.Popen([sys.executable, "-c", bridge, json.dumps(command), str(cwd)],
                                 stdin=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **kwargs)
        try:
            child._b71_job = WindowsJob(child.pid)
            child.stdin.write(b"1")
            child.stdin.close()
        except BaseException:
            child.stdin.close()
            if hasattr(child, "_b71_job"):
                child._b71_job.close()
            else:
                child.terminate()
            child.wait(timeout=5)
            raise
        return child
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def stop_child(child):
    if os.name == "nt":
        try:
            if child.poll() is None:
                try:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    pass  # The job still owns and terminates the full process tree.
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            child._b71_job.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise Failure("Owned Windows process did not stop after job termination", 4)
        return
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


def test_unit():
    preflight(frontend=True, backend=True)
    run(tool("uv", "run", "--no-sync", "pytest", "-m", "not db", "-q"), cwd=BACKEND, timeout=120)
    run(tool("npm", "run", "test:unit"), cwd=FRONTEND, timeout=120, env=command_env())


def checked_test_url():
    try:
        return guarded_url()
    except UnsafeTestDatabase as exc:
        raise Failure(str(exc), 2) from exc


def ensure_local_docker():
    """Reject TCP/SSH daemons, including a remote selected active context."""
    def local_endpoint(value):
        return value.startswith("unix:///") or value.startswith("npipe:////")

    selected = os.environ.get("DOCKER_HOST", "")
    if selected and not local_endpoint(selected):
        raise Failure("Database tests require a local Docker endpoint", 2)
    try:
        endpoint = json.loads(run(tool("docker", "context", "inspect", "--format",
                                       "{{json .Endpoints.docker.Host}}"), capture=True,
                                  timeout=15))
    except (Failure, ValueError) as exc:
        raise Failure("Cannot verify the local Docker context", 2) from exc
    if not isinstance(endpoint, str) or not local_endpoint(endpoint):
        raise Failure("Database tests require a local Docker context", 2)


def ensure_test_compose_target():
    """Check all routing inputs and the exact resolved Compose target before mutation."""
    if any(key.startswith("COMPOSE_") for key in os.environ):
        raise Failure("Database tests reject Compose environment overrides", 2)
    try:
        settings = read_settings(ENV)
    except UnsafeTestDatabase as exc:
        raise Failure(str(exc), 2) from exc
    if any(key.removeprefix("export ").strip().upper().startswith("COMPOSE_")
           for key in settings):
        raise Failure("Database tests reject Compose settings in .env", 2)
    ensure_local_docker()
    try:
        config = json.loads(compose("config", "--format", "json", capture=True, test=True))
        service = config["services"]["db-test"]
        port = service["ports"]
        mount = service["volumes"]
        volume = config["volumes"]["db-test-data"]
        expected_port = {"host_ip": "127.0.0.1", "target": 5432, "published": "55433"}
        expected_mount = {"type": "volume", "source": "db-test-data",
                          "target": "/var/lib/postgresql/data"}
        if (config["name"] != "b7-1" or set(config["services"]) != set(SERVICES)
                or len(port) != 1 or any(port[0].get(k) != v for k, v in expected_port.items())
                or len(mount) != 1 or any(mount[0].get(k) != v for k, v in expected_mount.items())
                or volume["name"] != "b7-1_db-test-data"):
            raise ValueError("unexpected Compose target")
    except (Failure, KeyError, TypeError, ValueError) as exc:
        raise Failure("Database test Compose target does not match the dedicated local service", 2) from exc


def test_db_state():
    try:
        raw = compose("ps", "--all", "--format", "json", "db-test", capture=True, test=True)
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        running = [record.get("ID") for record in records
                   if record.get("Service") == "db-test" and record.get("State") == "running"]
        if len(running) > 1 or any(not item for item in running):
            raise ValueError("ambiguous container")
        return running[0] if running else None
    except (Failure, AttributeError, TypeError, ValueError) as exc:
        raise Failure("Cannot identify the dedicated database test container", 2) from exc


def inspect_test_container(container_id):
    """Verify the actual container before any SQL connection is attempted."""
    try:
        raw = run(tool("docker", "inspect", "--type", "container", container_id),
                  capture=True, timeout=15)
        details, = json.loads(raw)
        labels = details["Config"]["Labels"]
        mounts = details["Mounts"]
        ports = details["NetworkSettings"]["Ports"]
        bindings = details["HostConfig"]["PortBindings"]
        expected_port = [{"HostIp": "127.0.0.1", "HostPort": "55433"}]
        if (len(container_id) < 12 or not details["Id"].startswith(container_id)
                or not details["State"]["Running"]
                or labels["com.docker.compose.project"] != "b7-1"
                or labels["com.docker.compose.service"] != "db-test"
                or labels["com.docker.compose.project.working_dir"] != str(ROOT)
                or labels["com.docker.compose.project.config_files"] != str(ROOT / "compose.yaml")
                or len(mounts) != 1
                or mounts[0]["Type"] != "volume"
                or mounts[0]["Name"] != "b7-1_db-test-data"
                or mounts[0]["Destination"] != "/var/lib/postgresql/data"
                or ports != {"5432/tcp": expected_port}
                or bindings != {"5432/tcp": expected_port}):
            raise ValueError("unexpected container identity or binding")
        return details["Id"]
    except (Failure, IndexError, KeyError, TypeError, ValueError) as exc:
        raise Failure("Cannot verify dedicated database test container ownership", 2) from exc


def wait_test_database(container_id):
    """Use the inspected container, even if Compose service membership changes."""
    try:
        for _ in range(30):
            state = json.loads(run(tool("docker", "inspect", "--type", "container",
                                      "--format", "{{json .State}}", container_id),
                                   capture=True, timeout=15))
            if not state["Running"]:
                raise Failure("Inspected database test container stopped", 4)
            if state.get("Health", {}).get("Status") == "healthy":
                break
            time.sleep(2)
        else:
            raise Failure("db-test did not become healthy within 60 seconds", 4)
        result = run(tool("docker", "exec", "-i", container_id, "sh", "-c",
                          'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT 1"'),
                     capture=True, timeout=15)
        if result != "1":
            raise Failure("db-test: SELECT 1 returned an unexpected result", 4)
    except (Failure, KeyError, TypeError, ValueError) as exc:
        raise Failure("Cannot verify the inspected database test container", 4) from exc
    print("db-test: SELECT 1 passed")


def verify_host_database(url, container_id):
    """Find the live host connection inside this container's pg_stat_activity."""
    try:
        script = '''import os, secrets, subprocess, psycopg
marker = "b71-" + secrets.token_hex(16)
with psycopg.connect(os.environ["TEST_DATABASE_URL"], connect_timeout=3,
                     application_name=marker) as connection:
    pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
    query = ("SELECT count(*) FROM pg_stat_activity WHERE pid = " + str(pid)
             + " AND application_name = '" + marker + "'")
    command = [os.environ["B71_PROBE_DOCKER"], "exec", "-i",
               os.environ["B71_PROBE_CONTAINER"], "sh", "-c",
               'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "$1"',
               "sh", query]
    result = subprocess.run(command, text=True, capture_output=True, timeout=10)
    print("1" if result.returncode == 0 and result.stdout.strip() == "1" else "0")
'''
        env = command_env()
        env["TEST_DATABASE_URL"] = url
        env["B71_PROBE_DOCKER"] = TOOLS["docker"]
        env["B71_PROBE_CONTAINER"] = container_id
        result = run(tool("uv", "run", "--no-sync", "python", "-c", script),
                     cwd=BACKEND, capture=True, timeout=20, env=env)
        if result != "1":
            raise ValueError("host connection was not found in db-test")
    except (Failure, ValueError) as exc:
        raise Failure("Cannot verify that the database test host port belongs to db-test", 2) from exc


def started_test_container():
    """Resolve a successful Compose up without guessing a replacement's owner."""
    try:
        try:
            candidate = test_db_state()
        except Failure:
            candidate = None
        if not candidate:
            raw = compose("ps", "-q", "db-test", capture=True, test=True)
            candidates = raw.splitlines()
            if len(candidates) != 1:
                raise ValueError("ambiguous started container")
            candidate = candidates[0]
        return inspect_test_container(candidate)
    except (Failure, ValueError) as exc:
        raise Failure("Cannot identify started db-test; it may remain running and requires manual inspection", 2) from exc


def stop_test_container(container_id):
    try:
        run(tool("docker", "stop", container_id), capture=True, timeout=30)
    except Failure as exc:
        raise Failure("Could not stop the container started by this command; inspect db-test manually", 2) from exc


def with_test_database(action):
    url = checked_test_url()  # never start a service before validating the exact target
    ensure_test_compose_target()
    preflight(docker=True, backend=True)
    initial_id = test_db_state()
    started_id = None
    try:
        if not initial_id:
            compose("up", "-d", "db-test", timeout=300, test=True)
            started_id = started_test_container()
        container_id = started_id or inspect_test_container(initial_id)
        wait_test_database(container_id)
        verify_host_database(url, container_id)
        current = test_db_state()
        if not current or not container_id.startswith(current):
            raise Failure("Database test service changed during ownership verification", 2)
        action(url)
    finally:
        if started_id:
            stop_test_container(started_id)


def test_db():
    def check(url):
        env = command_env()
        env["TEST_DATABASE_URL"] = url
        run(tool("uv", "run", "--no-sync", "pytest", "-m", "db", "-q"),
            cwd=BACKEND, timeout=120, env=env)
    with_test_database(check)


def require_free_ports():
    for port in (8000, 5173):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise Failure(f"Local port {port} is occupied or unavailable; test servers were not started", 2) from exc


def wait_http(url, expected):
    for _ in range(30):
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == expected:
                    return
        except (URLError, HTTPError, TimeoutError):
            pass
        time.sleep(0.5)
    raise Failure("Owned test server did not become ready", 4)


def test_e2e():
    preflight(frontend=True, backend=True)
    def check(url):
        require_free_ports()
        api_env = command_env()
        api_env["DATABASE_URL"] = url
        browser_env = command_env()
        browser_env["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".cache" / "playwright")
        children = []
        try:
            children.append(spawn(tool("uv", "run", "--no-sync", "python", "-m", "uvicorn",
                                       "app.main:app", "--host", "127.0.0.1", "--port", "8000"),
                                  BACKEND, env=api_env))
            wait_http("http://127.0.0.1:8000/api/ready", 200)
            children.append(spawn(tool("npm", "run", "dev", "--", "--port", "5173", "--strictPort"),
                                  FRONTEND, env=browser_env))
            wait_http("http://127.0.0.1:5173/", 200)
            run(tool("npm", "run", "test:e2e"), cwd=FRONTEND, timeout=180, env=browser_env)
        finally:
            for child in reversed(children):
                stop_child(child)
    with_test_database(check)


def test_all():
    test_unit()
    test_db()
    test_e2e()


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
    parser.add_argument("command", choices=("setup", "dev", "check", "quality", "build", "stop", "clean",
                                            "test", "test-unit", "test-db", "test-e2e"))
    args = parser.parse_args()
    try:
        {"setup": setup, "dev": dev, "check": checks, "quality": quality, "build": build,
         "stop": stop, "clean": clean, "test": test_all, "test-unit": test_unit,
         "test-db": test_db, "test-e2e": test_e2e}[args.command]()
    except Failure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
