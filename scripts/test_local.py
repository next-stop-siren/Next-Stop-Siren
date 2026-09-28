"""Safe automation scenarios; all mutable state is temporary."""

import ctypes
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import local


class LocalAutomationTest(unittest.TestCase):
    def test_windows_tool_resolves_command_wrapper(self):
        with patch.object(local.os, "name", "nt"), \
             patch.object(local.shutil, "which", return_value=r"C:\Tools\npm.cmd"):
            self.assertEqual(local.tool("npm", "--version"),
                             [r"C:\Tools\npm.cmd", "--version"])

    def test_clean_removes_only_generated_caches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frontend = root / "frontend"
            backend = root / "backend"
            scripts = root / "scripts"
            for path in (frontend / "dist", frontend / "node_modules" / ".vite",
                         backend / "app" / "__pycache__", backend / "tests" / "__pycache__",
                         backend / ".venv", scripts / "__pycache__"):
                path.mkdir(parents=True)
            (root / ".env").write_text("private", encoding="utf-8")
            (frontend / "node_modules" / "keep").write_text("installed", encoding="utf-8")
            (frontend / "package.json").write_text(
                '{"scripts":{"clean":"node -e \\\"const fs=require(\'node:fs\'); for (const p of [\'dist\',\'node_modules/.vite\']) fs.rmSync(p,{recursive:true,force:true})\\\""}}',
                encoding="utf-8")
            (backend / "scripts").mkdir()
            shutil.copyfile(local.BACKEND / "scripts" / "clean_cache.py", backend / "scripts" / "clean_cache.py")
            with patch.object(local, "ROOT", root), patch.object(local, "FRONTEND", frontend), \
                 patch.object(local, "BACKEND", backend), patch.object(local, "preflight"):
                local.clean()
            for path in (frontend / "dist", frontend / "node_modules" / ".vite",
                         backend / "app" / "__pycache__", backend / "tests" / "__pycache__",
                         scripts / "__pycache__"):
                self.assertFalse(path.exists(), str(path))
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "private")
            self.assertTrue((frontend / "node_modules" / "keep").exists())
            self.assertTrue((backend / ".venv").exists())

    def test_failed_preflight_leaves_existing_state_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env.example").write_text("example", encoding="utf-8")
            with patch.object(local, "ROOT", root), patch.object(local, "ENV", root / ".env"), \
                 patch.object(local, "preflight", side_effect=local.Failure("daemon unavailable", 2)), \
                 patch.object(local, "run") as command:
                with self.assertRaises(local.Failure):
                    local.setup()
                self.assertFalse((root / ".env").exists())
                command.assert_not_called()

    def test_repeat_setup_preserves_env_and_rechecks_both_databases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / ".env"
            env.write_text("private existing value", encoding="utf-8")
            calls = []

            def fake_compose(*args, **_kwargs):
                calls.append(args)
                if args[:2] == ("ps", "--format"):
                    return '{"Health":"healthy"}'
                if args[:2] == ("exec", "-T"):
                    return "1"
                return ""

            with patch.object(local, "ROOT", root), patch.object(local, "ENV", env), \
                 patch.object(local, "preflight"), patch.object(local, "run") as command, \
                 patch.object(local, "compose", side_effect=fake_compose), \
                 patch.object(local, "api_probe"), patch.object(local, "checks"):
                local.setup()
                local.setup()
            self.assertEqual(env.read_text(encoding="utf-8"), "private existing value")
            self.assertEqual(sum(call[:2] == ("up", "-d") for call in calls), 2)
            self.assertEqual(sum(call[:2] == ("exec", "-T") for call in calls), 4)
            self.assertEqual(sum("playwright" in call.args[0] and "chromium" in call.args[0]
                                 for call in command.call_args_list), 2)

    def test_rejected_test_database_never_starts_compose(self):
        with patch.object(local, "checked_test_url", side_effect=local.Failure("unsafe target", 2)), \
             patch.object(local, "compose") as compose:
            with self.assertRaises(local.Failure):
                local.test_db()
            compose.assert_not_called()

    def test_test_database_restores_initially_stopped_service(self):
        calls = []
        with patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "ensure_test_compose_target"), \
             patch.object(local, "preflight"), \
             patch.object(local, "test_db_state", side_effect=[None, "123456789abc", "123456789abc"]), \
             patch.object(local, "inspect_test_container", return_value="123456789abc0000") as inspect, \
             patch.object(local, "wait_test_database") as sql, \
             patch.object(local, "verify_host_database") as host, \
             patch.object(local, "stop_test_container") as stop, \
             patch.object(local, "compose", side_effect=lambda *args, **kwargs: calls.append(args)):
            local.with_test_database(lambda url: self.assertEqual(url, "validated"))
        self.assertEqual(calls, [("up", "-d", "db-test")])
        inspect.assert_called_once_with("123456789abc")
        sql.assert_called_once_with("123456789abc0000")
        host.assert_called_once_with("validated", "123456789abc0000")
        stop.assert_called_once_with("123456789abc0000")

    def test_test_database_preserves_initially_running_service(self):
        with patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "ensure_test_compose_target"), \
             patch.object(local, "preflight"), \
             patch.object(local, "test_db_state", return_value="123456789abc"), \
             patch.object(local, "inspect_test_container", return_value="123456789abc0000") as inspect, \
             patch.object(local, "wait_test_database") as sql, \
             patch.object(local, "verify_host_database") as host, \
             patch.object(local, "compose") as compose:
            local.with_test_database(lambda url: self.assertEqual(url, "validated"))
            compose.assert_not_called()
            inspect.assert_called_once_with("123456789abc")
            sql.assert_called_once_with("123456789abc0000")
            host.assert_called_once_with("validated", "123456789abc0000")

    def test_running_container_stale_port_or_mount_fails_before_db_access(self):
        good = {"Id": "123456789abc0000", "State": {"Running": True},
                "Config": {"Labels": {
                    "com.docker.compose.project": "b7-1",
                    "com.docker.compose.service": "db-test",
                    "com.docker.compose.project.working_dir": str(local.ROOT),
                    "com.docker.compose.project.config_files": str(local.ROOT / "compose.yaml")}},
                "Mounts": [{"Type": "volume", "Name": "b7-1_db-test-data",
                            "Destination": "/var/lib/postgresql/data"}],
                "NetworkSettings": {"Ports": {"5432/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "55433"}]}},
                "HostConfig": {"PortBindings": {"5432/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "55433"}]}}}
        for field in ("port", "mount"):
            with self.subTest(field=field):
                details = json.loads(json.dumps(good))
                if field == "port":
                    details["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"] = "55434"
                else:
                    details["Mounts"][0]["Name"] = "foreign-volume"
                with patch.object(local, "checked_test_url", return_value="validated"), \
                     patch.object(local, "ensure_test_compose_target"), \
                     patch.object(local, "preflight"), \
                     patch.object(local, "test_db_state", return_value="123456789abc"), \
                     patch.object(local, "run", return_value=json.dumps([details])), \
                     patch.object(local, "wait_test_database") as sql, \
                     patch.object(local, "verify_host_database") as host, \
                     patch.object(local, "compose") as compose:
                    with self.assertRaisesRegex(local.Failure, "container ownership"):
                        local.with_test_database(lambda _: self.fail("action reached"))
                    sql.assert_not_called()
                    host.assert_not_called()
                    compose.assert_not_called()

    def test_foreign_host_port_fails_before_action_and_preserves_running_service(self):
        with patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "ensure_test_compose_target"), \
             patch.object(local, "preflight"), \
             patch.object(local, "test_db_state", return_value="123456789abc"), \
             patch.object(local, "inspect_test_container", return_value="123456789abc0000"), \
             patch.object(local, "wait_test_database"), \
             patch.object(local, "verify_host_database", side_effect=local.Failure("foreign port", 2)), \
             patch.object(local, "compose") as compose:
            with self.assertRaises(local.Failure):
                local.with_test_database(lambda _: self.fail("action reached"))
            compose.assert_not_called()

    def test_service_swap_before_host_proof_never_reaches_action(self):
        events = []
        with patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "ensure_test_compose_target"), \
             patch.object(local, "preflight"), \
             patch.object(local, "test_db_state", side_effect=["123456789abc", "fedcba987654"]), \
             patch.object(local, "inspect_test_container", return_value="123456789abc0000"), \
             patch.object(local, "wait_test_database", side_effect=lambda cid: events.append(("sql", cid))), \
             patch.object(local, "verify_host_database", side_effect=lambda url, cid: events.append(("host", cid))), \
             patch.object(local, "compose") as compose:
            with self.assertRaisesRegex(local.Failure, "service changed"):
                local.with_test_database(lambda _: self.fail("action reached"))
            compose.assert_not_called()
        self.assertEqual(events, [("sql", "123456789abc0000"),
                                  ("host", "123456789abc0000")])

    def test_service_swap_during_cleanup_stops_only_started_id(self):
        current = {"id": "123456789abc"}
        calls = []
        def state():
            if not calls:
                calls.append("initial")
                return None
            return current["id"]
        def command(argv, **_kwargs):
            self.assertEqual(argv[-2:], ["stop", "123456789abc0000"])
            calls.append("stopped-owned-id")
            return "123456789abc0000"
        with patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "ensure_test_compose_target"), \
             patch.object(local, "preflight"), \
             patch.object(local, "test_db_state", side_effect=state), \
             patch.object(local, "inspect_test_container", return_value="123456789abc0000"), \
             patch.object(local, "wait_test_database"), \
             patch.object(local, "verify_host_database"), \
             patch.object(local, "compose"), \
             patch.object(local, "run", side_effect=command):
            local.with_test_database(lambda _: current.update(id="fedcba987654"))
        self.assertIn("stopped-owned-id", calls)

    def test_up_success_with_missing_ps_id_uses_exact_fallback_or_reports_residual(self):
        with patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "ensure_test_compose_target"), \
             patch.object(local, "preflight"), \
             patch.object(local, "test_db_state", side_effect=[None, None, "123456789abc"]), \
             patch.object(local, "compose", side_effect=lambda *args, **_: "123456789abc0000" if args[:2] == ("ps", "-q") else ""), \
             patch.object(local, "inspect_test_container", return_value="123456789abc0000"), \
             patch.object(local, "wait_test_database"), \
             patch.object(local, "verify_host_database"), \
             patch.object(local, "stop_test_container") as stop:
            local.with_test_database(lambda _: None)
            stop.assert_called_once_with("123456789abc0000")
        with patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "ensure_test_compose_target"), \
             patch.object(local, "preflight"), \
             patch.object(local, "test_db_state", side_effect=[None, None]), \
             patch.object(local, "compose", return_value=""), \
             patch.object(local, "stop_test_container") as stop:
            with self.assertRaisesRegex(local.Failure, "may remain running"):
                local.with_test_database(lambda _: self.fail("action reached"))
            stop.assert_not_called()

    def test_host_database_identity_mismatch_fails_closed(self):
        with patch.object(local, "run", return_value="0") as command:
            with self.assertRaisesRegex(local.Failure, "host port belongs to db-test"):
                local.verify_host_database("validated", "123456789abc0000")
            self.assertEqual(command.call_args.kwargs["env"]["TEST_DATABASE_URL"], "validated")
            self.assertEqual(command.call_args.kwargs["env"]["B71_PROBE_CONTAINER"], "123456789abc0000")

    def test_shell_compose_routing_rejected_before_service_command(self):
        for key, value in (("COMPOSE_FILE", "/tmp/alternate.yaml"),
                           ("COMPOSE_PROJECT_NAME", "alternate"),
                           ("COMPOSE_ENV_FILES", "/tmp/alternate.env")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}), \
                 patch.object(local, "checked_test_url", return_value="validated"), \
                 patch.object(local, "compose") as compose:
                with self.assertRaisesRegex(local.Failure, "Compose environment overrides"):
                    local.with_test_database(lambda _: None)
                compose.assert_not_called()

    def test_env_compose_routing_rejected_before_service_command(self):
        for key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_ENV_FILES",
                    "export COMPOSE_FILE"):
            with self.subTest(key=key), patch.object(local, "checked_test_url", return_value="validated"), \
                 patch.object(local, "read_settings", return_value={key: "alternate"}), \
                 patch.object(local, "compose") as compose:
                with self.assertRaisesRegex(local.Failure, "Compose settings in .env"):
                    local.with_test_database(lambda _: None)
                compose.assert_not_called()

    def test_remote_docker_host_and_context_rejected_before_compose(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote.example:2376"}), \
             patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "read_settings", return_value={}), \
             patch.object(local, "compose") as compose:
            with self.assertRaisesRegex(local.Failure, "local Docker endpoint"):
                local.with_test_database(lambda _: None)
            compose.assert_not_called()
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "remote"}), \
             patch.object(local, "checked_test_url", return_value="validated"), \
             patch.object(local, "read_settings", return_value={}), \
             patch.object(local, "run", return_value='"ssh://remote.example"'), \
             patch.object(local, "compose") as compose:
            with self.assertRaisesRegex(local.Failure, "local Docker context"):
                local.with_test_database(lambda _: None)
            compose.assert_not_called()

    def test_fixed_manifest_ignores_default_override_and_rejects_wrong_project(self):
        config = {"name": "alternate", "services": {"db-dev": {}, "db-test": {
            "ports": [{"host_ip": "127.0.0.1", "target": 5432, "published": "55433"}],
            "volumes": [{"type": "volume", "source": "db-test-data",
                         "target": "/var/lib/postgresql/data"}]}},
            "volumes": {"db-test-data": {"name": "b7-1_db-test-data"}}}
        with patch.object(local, "read_settings", return_value={}), \
             patch.object(local, "ensure_local_docker"), \
             patch.object(local, "compose", return_value=json.dumps(config)) as compose:
            with self.assertRaisesRegex(local.Failure, "dedicated local service"):
                local.ensure_test_compose_target()
            compose.assert_called_once_with("config", "--format", "json", capture=True, test=True)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / ".env"
            (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            (root / "compose.override.yaml").write_text(
                "services:\n  unexpected-service:\n    image: busybox\n", encoding="utf-8")
            env.write_text("TEST_DB_NAME=example\n", encoding="utf-8")
            with patch.object(local, "ROOT", root), patch.object(local, "ENV", env), \
                 patch.object(local, "run", return_value="{}") as command:
                local.compose("config", "--format", "json", capture=True, test=True)
            argv = command.call_args.args[0]
            self.assertEqual(argv[argv.index("-f") + 1], str(root / "compose.yaml"))
            self.assertNotIn(str(root / "compose.override.yaml"), argv)
            self.assertEqual(argv[argv.index("-p") + 1], "b7-1")
            self.assertEqual(argv[argv.index("--project-directory") + 1], str(root))
            self.assertEqual(argv[argv.index("--env-file") + 1], str(env))

    def test_windows_job_assignment_precedes_launch_and_survives_launcher_exit(self):
        events = []

        class FakeStdin:
            def write(self, data):
                events.append(("gate", data))

            def close(self):
                events.append(("stdin-close",))

        class FakeChild:
            pid = 43210
            stdin = FakeStdin()

            def poll(self):
                return 0  # launcher already exited; descendants remain in its job

            def wait(self, timeout):
                return 0

        class FakeJob:
            def __init__(self, pid):
                events.append(("assigned", pid))

            def close(self):
                events.append(("job-closed",))

        child = FakeChild()
        with patch.object(local.os, "name", "nt"), patch.object(local, "command_env", return_value={}), \
             patch.object(local.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
             patch.object(local.subprocess, "Popen", return_value=child), \
             patch.object(local, "WindowsJob", FakeJob):
            launched = local.spawn(["example"], Path("."))
            local.stop_child(launched)
        self.assertEqual(events, [("assigned", 43210), ("gate", b"1"),
                                  ("stdin-close",), ("job-closed",)])

    def test_windows_job_assignment_failure_does_not_open_gate(self):
        events = []

        class FakeStdin:
            def close(self):
                events.append("stdin-close")

            def write(self, _data):
                events.append("gate")

        class FakeChild:
            pid = 12345
            stdin = FakeStdin()

            def terminate(self):
                events.append("terminated")

            def wait(self, timeout):
                events.append("waited")

        with patch.object(local.os, "name", "nt"), patch.object(local, "command_env", return_value={}), \
             patch.object(local.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
             patch.object(local.subprocess, "Popen", return_value=FakeChild()), \
             patch.object(local, "WindowsJob", side_effect=OSError("assignment rejected")):
            with self.assertRaises(OSError):
                local.spawn(["example"], Path("."))
        self.assertEqual(events, ["stdin-close", "terminated", "waited"])

    @unittest.skipUnless(os.name == "nt", "Windows runtime not available")
    def test_windows_job_kills_descendant_after_launcher_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "child-pid"
            grandchild = ("import os,pathlib,time; "
                          f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)")
            script = ("import pathlib,subprocess,sys; "
                      f"subprocess.Popen([sys.executable,'-c',{grandchild!r}])")
            child = local.spawn([sys.executable, "-c", script], Path(temp))
            child.wait(timeout=5)
            process = None
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.WaitForSingleObject.restype = ctypes.c_ulong
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            try:
                for _ in range(30):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(marker.exists())
                process = kernel.OpenProcess(0x00100000, False, int(marker.read_text()))
                self.assertTrue(process)
            finally:
                local.stop_child(child)
            try:
                self.assertEqual(kernel.WaitForSingleObject(process, 5000), 0)
            finally:
                if process:
                    kernel.CloseHandle(process)

    @unittest.skipIf(os.name == "nt", "POSIX process-group verification")
    def test_stop_child_signals_its_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "descendant-stopped"
            grandchild = ("import signal,time,pathlib; "
                          f"p=pathlib.Path({str(marker)!r}); "
                          "signal.signal(signal.SIGTERM, lambda *_: (p.write_text('stopped'), exit(0))); "
                          "time.sleep(30)")
            parent = ("import subprocess,sys,time; "
                      f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
                      "time.sleep(30)")
            child = local.spawn([sys.executable, "-c", parent], Path(temp))
            try:
                time.sleep(0.5)
                local.stop_child(child)
                for _ in range(20):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                self.assertEqual(marker.read_text(encoding="utf-8"), "stopped")
            finally:
                local.stop_child(child)

    @unittest.skipIf(os.name == "nt", "POSIX process-group verification")
    def test_stop_child_cleans_descendant_after_parent_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "orphan-stopped"
            ready = Path(temp) / "descendant-ready"
            grandchild = ("import signal,time,pathlib; "
                          f"p=pathlib.Path({str(marker)!r}); r=pathlib.Path({str(ready)!r}); "
                          "signal.signal(signal.SIGTERM, lambda *_: (p.write_text('stopped'), exit(0))); "
                          "r.write_text('ready'); time.sleep(30)")
            parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{grandchild!r}])"
            child = local.spawn([sys.executable, "-c", parent], Path(temp))
            try:
                child.wait(timeout=5)
                for _ in range(20):
                    if ready.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(ready.exists())
                local.stop_child(child)
                for _ in range(20):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                self.assertEqual(marker.read_text(encoding="utf-8"), "stopped")
            finally:
                local.stop_child(child)


if __name__ == "__main__":
    unittest.main()
