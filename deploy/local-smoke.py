"""Disposable loopback production-image smoke. No DNS, ACME, AWS or real secrets."""

import argparse
import base64
from collections.abc import Callable
import http.client
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import time
from urllib.parse import quote
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "next-stop-siren.duckdns.org"
ORIGIN = "https://" + DOMAIN


def short_access_token(values: dict[str, str], user_id: str) -> str:
    """A test-only HS256 token using stdlib, accepted by the unchanged app verifier."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": "smoke-access"}
    claims = {
        "iss": "smoke",
        "aud": "smoke",
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + 5,
        "jti": str(uuid4()),
        "token_type": "access",
    }

    def encoded(value: bytes) -> bytes:
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    signed = b".".join(encoded(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims))
    signature = hmac.new(base64.b64decode(values["AUTH_ACCESS_KEY"]), signed, hashlib.sha256).digest()
    return (signed + b"." + encoded(signature)).decode()


MOCK_ANSWER = "모의 답변입니다."


def completed_answer(envelope: dict, question_id: str, expected: str) -> dict:
    """Require the target attempt's completed, nonempty answer and ownership link."""
    job, answer = envelope.get("job", {}), envelope.get("answer")
    if (
        job.get("state") != "completed"
        or job.get("question_message_id") != question_id
        or not isinstance(answer, dict)
        or not answer.get("id")
        or answer.get("id") != job.get("answer_message_id")
        or answer.get("role") != "assistant"
        or answer.get("status") != "completed"
        or answer.get("reply_to_message_id") != question_id
        or not isinstance(answer.get("content"), str)
        or not answer["content"].strip()
        or answer["content"] != expected
    ):
        raise RuntimeError("invalid_completed_answer")
    return answer


def await_completed_job(
    read_frame: Callable[[float], str | None],
    job_id: str,
    question_id: str,
    *,
    timeout_seconds: float = 40,
    clock: Callable[[], float] = time.monotonic,
    expected: str = MOCK_ANSWER,
) -> dict:
    """One overall deadline, matching target-job completion, and matching deltas."""
    deadline = clock() + timeout_seconds
    pieces = []
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("job_completion_deadline")
        event = read_frame(remaining)
        if clock() >= deadline:
            raise TimeoutError("job_completion_deadline")
        if event is None:
            raise RuntimeError("stream_ended_before_completion")
        lines = event.splitlines()
        kind = next((line[7:] for line in lines if line.startswith("event: ")), None)
        if kind not in {"delta", "job"}:
            continue  # Ping/limits/other-job traffic does not reset the deadline.
        data = json.loads("\n".join(line[6:] for line in lines if line.startswith("data: ")))
        if kind == "delta" and data.get("job_id") == job_id:
            text = data.get("text")
            if not isinstance(text, str) or not text:
                raise RuntimeError("invalid_delta")
            pieces.append(text)
        elif kind == "job" and data.get("job", {}).get("id") == job_id:
            state = data["job"].get("state")
            if state in {"queued", "paused", "running"}:
                continue
            answer = completed_answer(data, question_id, expected)
            if "".join(pieces) != answer["content"]:
                raise RuntimeError("completed_delta_mismatch")
            return data


def verify_stored_answer(page: dict, completed: dict, question_id: str, expected: str = MOCK_ANSWER) -> None:
    """A different/failed/pending assistant row must never count as persistence."""
    answer = completed_answer(completed, question_id, expected)
    matching = [item for item in page.get("items", []) if item.get("id") == answer["id"]]
    if len(matching) != 1:
        raise RuntimeError("stored_answer_missing")
    stored = {"job": completed["job"], "answer": matching[0]}
    completed_answer(stored, question_id, expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", choices=["colima"], required=True)
    parser.add_argument("--frontend-image", required=True)
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    if evidence == ROOT or ROOT in evidence.parents or evidence.exists():
        parser.error("Use a new evidence directory outside the product")
    evidence.mkdir(parents=True, mode=0o700)
    nonce = secrets.token_hex(6)
    project = "b71-issue73-smoke-" + nonce
    docker = ["docker", "--context", args.context]
    commands = []
    checks = []
    owned = False

    def run(command, *, check=True, timeout=180):
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
        commands.append({"argv": command, "exit": result.returncode})
        (evidence / "commands.json").write_text(json.dumps(commands, indent=2))
        if check and result.returncode:
            # Tool stdout may contain application data; keep it external, do not print it.
            (evidence / "failed-command.txt").write_text(result.stdout + result.stderr)
            raise RuntimeError("command_failed")
        return result.stdout

    def passed(name):
        checks.append(name)
        print("PASS", name, flush=True)

    result = {"project": project, "context": args.context, "checks": checks, "status": "FAIL"}
    try:
        endpoint = json.loads(run(docker + ["context", "inspect", args.context]))[0]["Endpoints"]["docker"]["Host"]
        if not endpoint.startswith("unix://"):
            raise RuntimeError("local_daemon_required")
        if run(docker + ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]).strip():
            raise RuntimeError("project_already_exists")
        for resource in ("volume", "network"):
            if run(docker + [resource, "ls", "-q", "--filter", "label=com.docker.compose.project=" + project]).strip():
                raise RuntimeError("project_resources_already_exist")
        for image in (args.frontend_image, args.backend_image):
            inspected = json.loads(run(docker + ["image", "inspect", image]))[0]
            if (inspected["Os"], inspected["Architecture"]) != ("linux", "amd64"):
                raise RuntimeError("amd64_images_required")
        passed("genuine_linux_amd64_images")
        values = {
            "POSTGRES_USER": "smoke",
            "POSTGRES_DB": "smoke",
            "POSTGRES_PASSWORD": secrets.token_hex(24),
            "AUTH_ENV": "production",
            "AUTH_COOKIE_SECURE": "true",
            "CHAT_WORKER": "on",
            "AUTH_API_ORIGIN": ORIGIN,
            "AUTH_ALLOWED_ORIGINS": ORIGIN,
            "AUTH_ACCESS_KID": "smoke-access",
            "AUTH_REFRESH_KID": "smoke-refresh",
            "AUTH_JWT_ISSUER": "smoke",
            "AUTH_JWT_AUDIENCE": "smoke",
        }
        values["DATABASE_URL"] = "postgresql://smoke:" + values["POSTGRES_PASSWORD"] + "@db:5432/smoke"
        for name in ("AUTH_ACCESS_KEY", "AUTH_REFRESH_KEY", "AUTH_REFRESH_DIGEST_KEY", "AUTH_CSRF_KEY"):
            values[name] = base64.b64encode(secrets.token_bytes(32)).decode()
        runtime = evidence / "runtime.env"
        runtime.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        runtime.chmod(0o600)
        release = evidence / "release.env"
        release.write_text(
            f"FRONTEND_IMAGE={args.frontend_image}\nBACKEND_IMAGE={args.backend_image}\nRUNTIME_ENV_FILE={runtime}\n"
        )
        certificates = evidence / "certificates" / "live" / DOMAIN
        certificates.mkdir(parents=True)
        run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=" + DOMAIN,
                "-keyout",
                str(certificates / "privkey.pem"),
                "-out",
                str(certificates / "fullchain.pem"),
            ]
        )
        (certificates / "privkey.pem").chmod(0o600)
        override = evidence / "smoke.yaml"
        override.write_text(f'''services:
  nginx:
    pull_policy: never
    restart: "no"
    labels: {{org.next-stop-siren.issue73.smoke: "{nonce}"}}
    ports: !override ["127.0.0.1:0:80", "127.0.0.1:0:443"]
    volumes: !override
      - acme-webroot:/var/www/acme:ro
      - {evidence / "certificates"}:/etc/letsencrypt:ro
  backend:
    pull_policy: never
    restart: "no"
    labels: {{org.next-stop-siren.issue73.smoke: "{nonce}"}}
  db:
    restart: "no"
    labels: {{org.next-stop-siren.issue73.smoke: "{nonce}"}}
''')
        compose = docker + [
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(runtime),
            "--env-file",
            str(release),
            "-f",
            str(ROOT / "deploy/compose.yaml"),
            "-f",
            str(override),
        ]
        config = json.loads(run(compose + ["config", "--format", "json"]))
        if config["services"]["backend"].get("ports") or config["services"]["db"].get("ports"):
            raise RuntimeError("private_ports_required")
        if any(port["host_ip"] != "127.0.0.1" for port in config["services"]["nginx"]["ports"]):
            raise RuntimeError("loopback_required")
        passed("compose_private_api_db_loopback_nginx")
        owned = True  # Only this freshly verified, unoccupied nonce project can be changed below.
        run(compose + ["up", "-d", "--wait", "db"], timeout=240)
        # Explicitly initialize ONLY this disposable, newly created DB. Never production startup DDL.
        run(
            compose
            + [
                "run",
                "--rm",
                "--no-deps",
                "backend",
                "python",
                "-c",
                "import os; from app.db_init import initialize_schema; initialize_schema(os.environ['DATABASE_URL'])",
            ]
        )
        run(compose + ["up", "-d", "backend", "nginx"])
        port = int(run(compose + ["port", "nginx", "443"]).strip().rsplit(":", 1)[1])

        def request(method, path, payload=None, token=None):
            connection = http.client.HTTPSConnection(
                "127.0.0.1", port, context=ssl._create_unverified_context(), timeout=25
            )
            headers = {"Host": DOMAIN, "Origin": ORIGIN}
            if token:
                headers["Authorization"] = "Bearer " + token
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
            response = connection.getresponse()
            data = response.read()
            status, response_headers = response.status, response.getheaders()
            connection.close()
            return status, response_headers, data

        def wait_ready():
            for _ in range(60):
                try:
                    if request("GET", "/api/ready")[0] == 200:
                        return
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(1)
            raise RuntimeError("readiness_failed")

        wait_ready()
        run(compose + ["exec", "-T", "nginx", "nginx", "-t"])
        assert request("GET", "/deep/ui/path")[0] == 200
        assert request("GET", "/assets/absent.js")[0] == 404
        status, headers, _ = request("GET", "/api/not-found")
        assert status == 404 and "application/json" in dict(headers).get("Content-Type", "")
        passed("tls_nginx_syntax_spa_asset404_api404")
        account = {"email": "smoke-" + nonce + "@example.test", "password": secrets.token_urlsafe(24)}
        status, headers, data = request("POST", "/api/auth/register", account)
        assert status == 201
        auth = json.loads(data)
        token, user_id = auth["access_token"], auth["user"]["id"]
        assert any(
            name.lower() == "set-cookie" and "Secure" in value and "HttpOnly" in value for name, value in headers
        )
        assert request("GET", "/api/me", token=token)[0] == 200
        passed("production_password_auth_secure_cookie_without_google")
        conversation = json.loads(request("POST", "/api/conversations", {}, token)[2])["conversation"]["id"]

        def stream(access, since=None):
            connection = http.client.HTTPSConnection(
                "127.0.0.1", port, context=ssl._create_unverified_context(), timeout=25
            )
            path = "/api/chat/events" + ("?since=" + quote(since, safe="") if since else "")
            connection.request("GET", path, headers={"Host": DOMAIN, "Authorization": "Bearer " + access})
            response = connection.getresponse()
            assert response.status == 200
            return connection, response

        def frame(response, *, connection=None, timeout_seconds=None):
            deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
            lines = []
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("frame_deadline")
                    if connection is None or connection.sock is None:
                        raise RuntimeError("stream_socket_missing")
                    connection.sock.settimeout(remaining)
                line = response.readline()
                if not line:
                    return None
                if line in (b"\n", b"\r\n"):
                    return b"".join(lines).decode()
                lines.append(line)

        started = time.monotonic()
        connection, response = stream(token)
        initial = frame(response)
        assert initial and "event: snapshot" in initial and time.monotonic() - started < 5
        since = json.loads(initial.split("data: ", 1)[1])["server_time"]
        admission = request(
            "POST",
            f"/api/conversations/{conversation}/messages",
            {"content": "합성 교통법 질문", "request_key": nonce},
            token,
        )
        assert admission[0] == 202
        accepted = json.loads(admission[2])
        job_id, question_id = accepted["job"]["id"], accepted["question"]["id"]
        completed = await_completed_job(
            lambda remaining: frame(response, connection=connection, timeout_seconds=remaining),
            job_id,
            question_id,
        )
        for _ in range(5):
            event = frame(response)
            if event is None:
                raise RuntimeError("stream_ended_before_ping")
            if ": ping" in event:
                break
        else:
            raise RuntimeError("ping_not_delivered")
        connection.close()
        status, _, stored = request("GET", f"/api/conversations/{conversation}/messages", token=token)
        assert status == 200
        verify_stored_answer(json.loads(stored), completed, question_id)
        passed("unbuffered_snapshot_delta_15s_ping_mock_stored_answer")
        # Signed short-lived synthetic access verifies expiry of an ALREADY OPEN subscription.
        short = short_access_token(values, str(user_id))
        connection, response = stream(short)
        assert "event: snapshot" in frame(response)
        started = time.monotonic()
        assert frame(response) is None and time.monotonic() - started < 10
        connection.close()
        passed("open_sse_ends_at_access_expiry")
        connection, response = stream(token, since)
        assert "event: snapshot" in frame(response)
        started = time.monotonic()
        run(compose + ["restart", "backend"], timeout=90)
        assert time.monotonic() - started < 60
        connection.close()
        wait_ready()
        connection, response = stream(token, since)
        assert "event: snapshot" in frame(response)
        connection.close()
        assert request("GET", f"/api/conversations/{conversation}/messages", token=token)[2] == stored
        passed("bounded_shutdown_open_sse_restart_reconnect_stored_state")
        sentinel = "synthetic-query-credential-" + nonce
        request("GET", "/api/auth/google/callback?code=" + sentinel + "&state=" + sentinel)
        run(compose + ["stop", "backend"], timeout=90)
        assert request("GET", "/api/error?code=" + sentinel)[0] in {502, 504}
        run(compose + ["start", "backend"])
        wait_ready()
        logs = run(compose + ["logs", "--no-color"])
        assert sentinel not in logs and token not in logs and account["password"] not in logs
        for name in ("AUTH_ACCESS_KEY", "AUTH_REFRESH_KEY", "AUTH_REFRESH_DIGEST_KEY", "AUTH_CSRF_KEY"):
            assert values[name] not in logs
        (evidence / "sanitized-container-logs.txt").write_text(logs)
        passed("query_cookie_auth_secrets_absent_from_access_error_logs")
        result["status"] = "PASS"
    except Exception as error:
        result["failure_type"] = type(error).__name__
        import traceback

        (evidence / "failure-stack.txt").write_text(traceback.format_exc())
        print("FAIL", type(error).__name__, "(values hidden)", flush=True)
    finally:
        if owned:
            try:
                ids = run(docker + ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]).split()
                if ids:
                    details = json.loads(run(docker + ["inspect", *ids]))
                    if any(
                        item["Config"]["Labels"].get("org.next-stop-siren.issue73.smoke") != nonce for item in details
                    ):
                        raise RuntimeError("cleanup_ownership_changed")
                run(compose + ["down", "--volumes", "--remove-orphans"], timeout=120)
                result["cleanup"] = "owned_project_removed"
            except Exception:
                result["cleanup"] = "FAILED_preserved_for_manual_inspection"
                result["status"] = "FAIL"
        (evidence / "result.json").write_text(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
