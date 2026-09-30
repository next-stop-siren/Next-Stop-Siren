"""Portable checks for the Windows entry's orchestration and report boundary."""

import json
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import local
import windows_check


class WindowsCheckTest(unittest.TestCase):
    def test_bootstrap_failure_template_is_bounded_and_covers_all_steps(self):
        template = Path(__file__).with_name("windows-bootstrap-failure.json")
        result = json.loads(template.read_text(encoding="utf-8"))
        self.assertEqual(set(result), {"os", "versions", "git_commit", "steps",
                                       "failure_phase", "exit_code"})
        self.assertEqual(result["os"], "Windows")
        self.assertEqual(result["versions"], {})
        self.assertEqual(result["git_commit"], "unavailable")
        self.assertEqual(result["failure_phase"], "host_bootstrap")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(set(result["steps"]), set(windows_check.STEPS))
        self.assertEqual(result["steps"]["preflight"], "FAIL")
        self.assertTrue(all(value == "NOT_RUN" for name, value in result["steps"].items()
                            if name != "preflight"))

    def run_with_failure(self, failed_step, failure):
        calls = []
        actions = {name: lambda name=name: calls.append(name) for name in windows_check.STEPS}

        def fail():
            calls.append(failed_step)
            raise failure

        actions[failed_step] = fail
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, redirect_stderr(output):
            report = Path(temp) / "report.json"
            code = windows_check.execute(report, steps=actions,
                                         info=lambda: {"os": "Windows", "versions": {},
                                                       "git_commit": "a" * 40})
            data = json.loads(report.read_text(encoding="utf-8"))
        return code, data, calls, output.getvalue()

    def test_interrupt_marks_active_step_failed_and_future_steps_not_run(self):
        code, data, calls, output = self.run_with_failure("lint", KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertEqual(calls, list(windows_check.STEPS[:5]))
        self.assertEqual(data["steps"]["format"], "PASS")
        self.assertEqual(data["steps"]["lint"], "FAIL")
        self.assertEqual(data["steps"]["typecheck"], "NOT_RUN")
        self.assertEqual(data["exit_code"], 130)
        self.assertIn("lint interrupted (exit 130)", output)

    def test_ambiguous_test_owner_has_fixed_manual_inspection_notice(self):
        for step in ("database", "browser_e2e"):
            with self.subTest(step=step):
                failure = local.Failure(windows_check.AMBIGUOUS_TEST_OWNER, 2)
                code, data, _, output = self.run_with_failure(step, failure)
                self.assertEqual(code, 2)
                self.assertEqual(data["steps"][step], "FAIL")
                self.assertIn("Inspect the B7-1 db-test service manually", output)
                self.assertNotIn(windows_check.AMBIGUOUS_TEST_OWNER, output)
                self.assertNotIn(windows_check.AMBIGUOUS_TEST_OWNER, json.dumps(data))

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
