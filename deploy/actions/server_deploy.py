"""Owner-provisioned root helper; public manifest only, never secrets in commands/logs."""

import argparse
import fcntl
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import urllib.request

from contract import (
    FILES,
    REPOSITORY,
    canonical,
    decode_envelope,
    digest,
    matches,
    read_json,
    require,
    validate_release,
)

ROOT = Path("/opt/next-stop-siren")
RUNTIME = Path("/etc/next-stop-siren/production.env")
GATES = Path("/etc/next-stop-siren/deploy-gates.json")
FUNCTIONAL = Path("/usr/local/lib/next-stop-siren/verify-pilot-readiness")


def trusted(path: Path, *, private=False) -> None:
    info = path.lstat()
    require(not path.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022, "root_owned_required")
    require(not private or stat.S_IMODE(info.st_mode) & 0o077 == 0, "private_runtime_required")


def fetch_compose(sha: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{sha}/{FILES['compose.yaml']}"
    with urllib.request.urlopen(url, timeout=30) as response:
        require(response.url == url, "compose_redirect")
        data = response.read(250_001)
    require(len(data) <= 250_000, "compose_size")
    return data


def atomic(path: Path, value: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(value)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def run_command(arguments: list[str], lock_fd: int, deadline: float) -> None:
    remaining = min(300, deadline - time.monotonic())
    require(remaining > 0, "host_deadline")
    process = subprocess.Popen(
        arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(lock_fd,), start_new_session=True
    )
    try:
        require(process.wait(timeout=remaining) == 0, "host_command_failed")
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise TimeoutError("host_command_timeout") from None


def verify_images(command: list[str], release: dict) -> bool:
    """Read only container/image IDs, never inspect Config.Env or command output."""

    def metadata(arguments):
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=15)
        require(result.returncode == 0, "image_identity_query")
        value = result.stdout.strip()
        require(matches(r"(?:sha256:)?[0-9a-f]{12,64}", value), "image_identity_shape")
        return value

    try:
        for service, name in (("nginx", "frontend"), ("backend", "backend")):
            container = metadata(command + ["ps", "--all", "--quiet", service])
            observed = metadata(
                ["docker", "--host", "unix:///var/run/docker.sock", "inspect", "--format", "{{.Image}}", container]
            )
            expected = metadata(
                [
                    "docker",
                    "--host",
                    "unix:///var/run/docker.sock",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    release[name],
                ]
            )
            if observed != expected:
                return False
        return True
    except (ValueError, subprocess.SubprocessError):
        return False


def deploy(
    data: dict,
    *,
    root=ROOT,
    runtime=RUNTIME,
    gates_path=GATES,
    functional=FUNCTIONAL,
    fetch=fetch_compose,
    runner=run_command,
    check_trusted=trusted,
    identity_check=verify_images,
) -> str:
    release = validate_release(data["release"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_trusted(root)
    with (root / "deploy.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Descriptor is also inherited by running child commands. Actions timeout never releases this lock.
        require(not (root / "inflight.json").exists(), "owner_reconciliation_required")
        for path in (runtime, gates_path, functional, Path(__file__), Path(__file__).with_name("contract.py")):
            check_trusted(path, private=path == runtime)
        gates = read_json(gates_path.read_bytes())
        require(
            digest(gates_path.read_bytes()) == data["gates_sha256"]
            and gates.get("schema_version") == 1
            and gates.get("source_sha") == release["source_sha"],
            "owner_gates_required",
        )
        for name in ("readiness", "trusted_proxy", "data_recovery"):
            evidence = gates.get(name, {})
            require(
                evidence.get("status") == "approved"
                and bool(evidence.get("evidence_ref"))
                and matches(r"[0-9a-f]{64}", evidence.get("sha256")),
                "external_operational_gate_" + name,
            )
        require(
            digest(functional.read_bytes()) == gates.get("functional_check_sha256") and os.access(functional, os.X_OK),
            "functional_check_required",
        )
        require(matches(r"certbot/certbot@sha256:[0-9a-f]{64}", gates.get("certbot_image")), "certbot_digest")
        require(
            digest(Path(__file__).read_bytes()) == release["files"]["server_deploy.py"]
            and digest(Path(__file__).with_name("contract.py").read_bytes()) == release["files"]["contract.py"],
            "preprovisioned_helper_revision",
        )
        deadline = time.monotonic() + 900
        identity = {"release_sha256": digest(canonical(release)), "gates_sha256": data["gates_sha256"]}
        current = root / "current-release.json"
        compose = fetch(release["source_sha"])
        require(digest(compose) == release["files"]["compose.yaml"], "compose_source_hash")
        directory = root / "releases" / (release["source_sha"] + "-" + identity["release_sha256"][:12])
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        check_trusted(directory)
        atomic(directory / "compose.yaml", compose)
        atomic(directory / "release.json", canonical(release))
        atomic(
            directory / "release.env",
            (
                f"FRONTEND_IMAGE={release['frontend']}\nBACKEND_IMAGE={release['backend']}\n"
                f"CERTBOT_IMAGE={gates['certbot_image']}\nRUNTIME_ENV_FILE={runtime}\n"
            ).encode(),
        )
        command = [
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "compose",
            "--project-name",
            "next-stop-siren",
            "--env-file",
            str(runtime),
            "--env-file",
            str(directory / "release.env"),
            "--file",
            str(directory / "compose.yaml"),
        ]
        functional_command = [
            str(functional),
            "--source",
            release["source_sha"],
            "--frontend",
            release["frontend"],
            "--backend",
            release["backend"],
        ]
        if current.exists() and read_json(current.read_bytes()) == identity and identity_check(command, release):
            runner(functional_command, lock.fileno(), deadline)
            return "already_applied_checked"
        runner(command + ["config", "--quiet"], lock.fileno(), deadline)
        runner(command + ["pull", "nginx", "backend", "db"], lock.fileno(), deadline)
        runner(command + ["run", "--rm", "--no-deps", "backend", "--validate-only"], lock.fileno(), deadline)
        atomic(root / "inflight.json", canonical(identity))  # Retained on failure/death; never blind retry.
        runner(command + ["up", "-d", "--no-recreate", "--wait", "db"], lock.fileno(), deadline)
        runner(command + ["up", "-d", "--no-deps", "backend"], lock.fileno(), deadline)
        runner(command + ["up", "-d", "--no-deps", "--force-recreate", "nginx"], lock.fileno(), deadline)
        require(identity_check(command, release), "running_image_mismatch")
        runner(functional_command, lock.fileno(), deadline)
        atomic(current, canonical(identity))
        (root / "inflight.json").unlink()
        return "applied_checked"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--manifest-base64", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = deploy(decode_envelope(args.manifest_base64))
    except Exception:
        print(
            "PILOT_DEPLOY_FAILED: stop; Owner must reconcile state; no automatic rollback/DB restore.", file=sys.stderr
        )
        return 1
    print("PILOT_DEPLOY_OK: " + result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
