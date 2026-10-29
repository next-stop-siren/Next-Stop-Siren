"""Production guard negative cases; stdlib runner, no network or database access."""

import base64
from copy import deepcopy
import json
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("production_entrypoint", ROOT / "deploy/backend-entrypoint.py")
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)
smoke_spec = importlib.util.spec_from_file_location("local_smoke", ROOT / "deploy/local-smoke.py")
smoke = importlib.util.module_from_spec(smoke_spec)
smoke_spec.loader.exec_module(smoke)


def synthetic_values() -> dict[str, str]:
    # Fixed synthetic values only; never use these for a running environment.
    values = {
        "AUTH_ENV": "production",
        "AUTH_COOKIE_SECURE": "true",
        "AUTH_API_ORIGIN": entrypoint.ORIGIN,
        "AUTH_ALLOWED_ORIGINS": entrypoint.ORIGIN,
        "AUTH_ACCESS_KID": "check-access",
        "AUTH_REFRESH_KID": "check-refresh",
        "AUTH_JWT_ISSUER": "check",
        "AUTH_JWT_AUDIENCE": "check",
        "POSTGRES_USER": "check",
        "POSTGRES_PASSWORD": "synthetic-only-password",
        "POSTGRES_DB": "check",
        "DATABASE_URL": "postgresql://check:synthetic-only-password@db:5432/check",
    }
    for index, name in enumerate(("AUTH_ACCESS_KEY", "AUTH_REFRESH_KEY", "AUTH_REFRESH_DIGEST_KEY", "AUTH_CSRF_KEY")):
        values[name] = base64.b64encode(bytes([index + 1]) * 32).decode()
    return values


class ProductionGuardChecks(unittest.TestCase):
    def test_password_auth_without_google(self):
        entrypoint.validate(synthetic_values())

    def test_unsafe_settings_fail(self):
        invalid = [
            {"AUTH_ENV": "development"},
            {"AUTH_COOKIE_SECURE": "false"},
            {"AUTH_ACCESS_KEY": ""},
            {"AUTH_ALLOWED_ORIGINS": "https://other.example"},
            {"DATABASE_URL": "postgresql://check:synthetic-only-password@127.0.0.1:55432/check"},
            {"DATABASE_URL": "postgresql+psycopg://check:synthetic-only-password@db:5432/check"},
            {"POSTGRES_PASSWORD": ""},
            {"CHAT_WORKER": "off"},
            {"AUTH_REGISTER_MAX_CONCURRENT": "0"},
            {"GOOGLE_CLIENT_ID": "partial.apps.googleusercontent.com"},
        ]
        for change in invalid:
            with self.subTest(fields=list(change)):
                with self.assertRaises(ValueError):
                    entrypoint.validate({**synthetic_values(), **change})

    def test_secret_build_context_and_proxy_contract(self):
        ignore = (ROOT / ".dockerignore").read_text()
        self.assertEqual(ignore.splitlines()[1], "**")
        for rule in ("**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/.venv", "**/node_modules"):
            self.assertIn(rule, ignore.splitlines())
        for filename in ("frontend.Dockerfile", "backend.Dockerfile"):
            dockerfile = (ROOT / "deploy" / filename).read_text()
            self.assertNotIn("COPY . ", dockerfile)
        nginx = (ROOT / "deploy/nginx/nginx.conf").read_text()
        self.assertIn("error_log /dev/null;", nginx)
        self.assertIn("proxy_buffering off;", nginx)
        self.assertIn("proxy_read_timeout 75s;", nginx)
        log_line = next(line for line in nginx.splitlines() if "log_format safe" in line)
        for forbidden in ("$request_uri", "$args", "$http_cookie", "$http_authorization", "$request "):
            self.assertNotIn(forbidden, log_line)


class SmokeCompletionChecks(unittest.TestCase):
    """Offline acceptance probes: failed/nonterminal/wrong answers cannot PASS."""

    @staticmethod
    def completed():
        return {
            "job": {"id": "1", "state": "completed", "question_message_id": "10", "answer_message_id": "11"},
            "answer": {
                "id": "11",
                "role": "assistant",
                "status": "completed",
                "content": smoke.MOCK_ANSWER,
                "reply_to_message_id": "10",
            },
        }

    @staticmethod
    def event(kind, data):
        return f"event: {kind}\ndata: {json.dumps(data)}\n\n"

    def delta(self, text=None):
        return self.event("delta", {"job_id": "1", "text": smoke.MOCK_ANSWER if text is None else text})

    def await_frames(self, events):
        frames = iter(events)
        return smoke.await_completed_job(lambda remaining: next(frames, None), "1", "10")

    def test_matching_completed_event_and_storage(self):
        expected = self.completed()
        completed = self.await_frames([self.delta(), self.event("job", expected)])
        smoke.verify_stored_answer({"items": [expected["answer"]]}, completed, "10")

    def test_nonterminal_stream_is_not_completion(self):
        running = {**self.completed(), "job": {**self.completed()["job"], "state": "running"}}
        events = [self.delta()] + [self.event("job", running)] * 100
        with self.assertRaises(RuntimeError):
            self.await_frames(events)

    def test_ping_only_uses_one_deadline(self):
        now, budgets = [0.0], []

        def ping(remaining):
            budgets.append(remaining)
            now[0] += 0.25
            return ": ping\n\n"

        with self.assertRaises(TimeoutError):
            smoke.await_completed_job(ping, "1", "10", timeout_seconds=1, clock=lambda: now[0])
        self.assertEqual(len(budgets), 4)
        self.assertEqual(budgets, [1.0, 0.75, 0.5, 0.25])

    def test_failed_or_interrupted_target_is_rejected(self):
        for state in ("failed", "interrupted", "canceled", "expired"):
            event = self.completed()
            event["job"]["state"] = state
            event["answer"].update(status="failed", content=None)
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                self.await_frames([self.delta(), self.event("job", event)])

    def test_other_job_cannot_complete_target(self):
        other = self.completed()
        other["job"]["id"] = "2"
        with self.assertRaises(RuntimeError):
            self.await_frames([self.delta(), self.event("job", other)])

    def test_wrong_completed_answer_or_delta_is_rejected(self):
        invalid = [
            {"status": "failed", "content": None},
            {"status": "pending"},
            {"status": "interrupted"},
            {"content": None},
            {"content": " "},
            {"content": "different answer"},
            {"id": "other"},
            {"role": "user"},
            {"reply_to_message_id": "other"},
        ]
        for change in invalid:
            event = self.completed()
            event["answer"].update(change)
            with self.subTest(fields=list(change)), self.assertRaises(RuntimeError):
                self.await_frames([self.delta(), self.event("job", event)])
        with self.assertRaises(RuntimeError):
            self.await_frames([self.delta("wrong delta"), self.event("job", self.completed())])

    def test_failed_or_wrong_stored_answer_is_rejected(self):
        completed = self.completed()
        for change in (
            {"status": "failed", "content": None},
            {"status": "pending"},
            {"content": "wrong"},
            {"content": ""},
            {"id": "another-answer"},
            {"role": "user"},
            {"reply_to_message_id": "another-question"},
        ):
            stored = deepcopy(completed["answer"])
            stored.update(change)
            with self.subTest(fields=list(change)), self.assertRaises(RuntimeError):
                smoke.verify_stored_answer({"items": [stored]}, completed, "10")


if __name__ == "__main__":
    unittest.main(verbosity=2)
