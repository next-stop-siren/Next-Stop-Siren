"""Portable checks for the Windows entry's orchestration and report boundary."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import local
import windows_check


class WindowsCheckTest(unittest.TestCase):
    def test_first_failure_stops_and_report_excludes_secrets(self):
        calls = []
        actions = {name: lambda name=name: calls.append(name) for name in windows_check.STEPS}

        def fail():
            calls.append("lint")
            raise local.Failure("private-password", 3)

        actions["lint"] = fail
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "report.json"
            with patch.dict(os.environ, {"TEST_DATABASE_URL": "postgresql://private-password"}):
                code = windows_check.execute(report, steps=actions,
                                             info=lambda: {"os": "Windows", "versions": {},
                                                           "git_commit": "a" * 40})
            data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(code, 3)
        self.assertEqual(calls, list(windows_check.STEPS[:5]))
        self.assertEqual(data["steps"]["lint"], "FAIL")
        self.assertEqual(data["steps"]["typecheck"], "NOT_RUN")
        self.assertNotIn("private-password", json.dumps(data))
        self.assertEqual(data["exit_code"], 3)

    def test_url_is_encoded_guarded_and_process_environment_restored(self):
        settings = {"TEST_DB_USER": "test user", "TEST_DB_PASSWORD": "pass@word",
                    "TEST_DB_NAME": "test/db"}
        with patch.object(local, "read_settings", return_value=settings), \
             patch.object(local, "checked_test_url", side_effect=lambda: os.environ["TEST_DATABASE_URL"]), \
             patch.dict(os.environ, {"TEST_DATABASE_URL": "previous"}):
            self.assertEqual(windows_check.test_url(),
                             "postgresql://test%20user:pass%40word@127.0.0.1:55433/test%2Fdb")
            self.assertEqual(os.environ["TEST_DATABASE_URL"], "previous")

    def test_setup_dependencies_and_inventory_preserve_initial_service_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / ".env"
            env.write_bytes(b"private=unchanged\r\n")
            before = env.stat().st_mode
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                return '{"ID":"initial-container"}' if "ps" in command else ""

            with patch.object(local, "ROOT", root), patch.object(local, "ENV", env), \
                 patch.object(local, "FRONTEND", root / "frontend"), \
                 patch.object(local, "BACKEND", root / "backend"), \
                 patch.object(local, "run", side_effect=run), \
                 patch.object(local, "compose") as compose:
                windows_check.inventory()
                local.setup_dependencies()
            self.assertEqual(env.read_bytes(), b"private=unchanged\r\n")
            self.assertEqual(env.stat().st_mode, before)
            self.assertEqual(sum("ps" in command for command in calls), 2)
            compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
