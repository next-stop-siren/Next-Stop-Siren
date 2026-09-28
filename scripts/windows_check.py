"""Bounded Windows verification with a shareable, secret-free result summary."""

import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
from urllib.parse import quote

import local


REPORT = local.ROOT / ".cache" / "windows-check" / "report.json"
STEPS = ("preflight", "inventory", "locked_setup", "format", "lint", "typecheck",
         "ruff_format", "ruff_lint", "mypy", "unit", "database", "browser_e2e", "build")


def test_url():
    """Keep the derived URL in this process; the existing guard decides if it is safe."""
    settings = local.read_settings(local.ENV)
    try:
        url = ("postgresql://" + quote(settings["TEST_DB_USER"], safe="") + ":" +
               quote(settings["TEST_DB_PASSWORD"], safe="") +
               "@127.0.0.1:55433/" + quote(settings["TEST_DB_NAME"], safe=""))
    except KeyError as exc:
        raise local.Failure("Missing dedicated test database setting", 2) from exc
    previous = os.environ.get("TEST_DATABASE_URL")
    os.environ["TEST_DATABASE_URL"] = url
    try:
        return local.checked_test_url()
    finally:
        if previous is None:
            os.environ.pop("TEST_DATABASE_URL", None)
        else:
            os.environ["TEST_DATABASE_URL"] = previous


def with_test_url(action):
    url = test_url()
    previous = os.environ.get("TEST_DATABASE_URL")
    os.environ["TEST_DATABASE_URL"] = url
    try:
        action()
    finally:
        if previous is None:
            os.environ.pop("TEST_DATABASE_URL", None)
        else:
            os.environ["TEST_DATABASE_URL"] = previous


def inventory():
    """Read relevant ports and Compose state before any local setup work."""
    for port in (55432, 55433, 8000, 5173):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                pass  # An occupied port is preserved; E2E has its own strict check.
    for service in local.SERVICES:
        raw = local.run(local.tool("docker", "ps", "--all",
                                   "--filter", "label=com.docker.compose.project=b7-1",
                                   "--filter", f"label=com.docker.compose.service={service}",
                                   "--format", "{{json .}}"), capture=True, timeout=15)
        for line in raw.splitlines():
            parsed = json.loads(line)
            if not isinstance(parsed, dict) or not parsed.get("ID"):
                raise local.Failure("Unexpected Docker service inventory", 2)


def version(name, args, pattern):
    value = local.run(local.tool(name, *args), capture=True,
                      env=local.command_env(), timeout=15)
    match = re.fullmatch(pattern, value)
    return match.group(1) if match else "unavailable"


def metadata():
    # Fixed patterns deliberately exclude paths, arbitrary command output and credentials.
    versions = {"python": ".".join(map(str, sys.version_info[:3]))}
    for name, args, pattern in (
        ("node", ("--version",), r"v?(\d+\.\d+\.\d+)"),
        ("npm", ("--version",), r"(\d+\.\d+\.\d+)"),
        ("uv", ("--version",), r"uv (\d+\.\d+\.\d+)"),
        ("docker", ("compose", "version", "--short"), r"v?(\d+\.\d+\.\d+)"),
        ("docker", ("info", "--format", "{{.ServerVersion}}"), r"v?(\d+\.\d+\.\d+)"),
    ):
        key = "compose" if args[0] == "compose" else "docker" if args[0] == "info" else name
        try:
            versions[key] = version(name, args, pattern)
        except local.Failure:
            versions[key] = "unavailable"
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=local.ROOT,
                                capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unavailable"
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        commit = "unavailable"
    return {"os": platform.system(), "versions": versions, "git_commit": commit}


def quality(exe, cwd, *args):
    local.run(local.tool(exe, *args), cwd=cwd, timeout=180, env=local.command_env())


def write_report(report_path, result):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(report_path)


def execute(report_path=REPORT, *, steps=None, info=None):
    state = {step: "NOT_RUN" for step in STEPS}
    result = {"os": "unknown", "versions": {}, "git_commit": "unavailable",
              "steps": state, "exit_code": 1}
    actions = steps or {
        "preflight": lambda: local.preflight(docker=True, frontend=True, backend=True),
        "inventory": inventory,
        "locked_setup": local.setup_dependencies,
        "format": lambda: quality("npm", local.FRONTEND, "run", "format:check"),
        "lint": lambda: quality("npm", local.FRONTEND, "run", "lint"),
        "typecheck": lambda: quality("npm", local.FRONTEND, "run", "typecheck"),
        "ruff_format": lambda: quality("uv", local.BACKEND, "run", "--no-sync", "ruff",
                                      "format", "--check", "app", "tests", "scripts"),
        "ruff_lint": lambda: quality("uv", local.BACKEND, "run", "--no-sync", "ruff",
                                    "check", "app", "tests", "scripts"),
        "mypy": lambda: quality("uv", local.BACKEND, "run", "--no-sync", "mypy", "app"),
        "unit": local.test_unit,
        "database": lambda: with_test_url(local.test_db),
        "browser_e2e": lambda: with_test_url(local.test_e2e),
        "build": local.build,
    }
    try:
        result.update(info() if info else metadata())
        for step in STEPS:
            try:
                actions[step]()
            except local.Failure as exc:
                state[step] = "FAIL"
                result["exit_code"] = exc.code
                print(f"{step} failed (exit {exc.code})", file=sys.stderr)
                break
            except Exception as exc:
                state[step] = "FAIL"
                result["exit_code"] = 2
                print(f"{step} failed: {type(exc).__name__}", file=sys.stderr)
                break
            else:
                state[step] = "PASS"
        else:
            result["exit_code"] = 0
    except KeyboardInterrupt:
        result["exit_code"] = 130
    finally:
        write_report(report_path, result)
    print("Result summary: .cache/windows-check/report.json")
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(execute())
