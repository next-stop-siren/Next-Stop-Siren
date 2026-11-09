"""Production guard negative cases; stdlib runner, no network or database access."""

import base64
from copy import deepcopy
import json
import importlib.util
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("production_entrypoint", ROOT / "deploy/backend-entrypoint.py")
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)
smoke_spec = importlib.util.spec_from_file_location("local_smoke", ROOT / "deploy/local-smoke.py")
smoke = importlib.util.module_from_spec(smoke_spec)
smoke_spec.loader.exec_module(smoke)
client_spec = importlib.util.spec_from_file_location("proxy_client", ROOT / "deploy/login-proxy-client.py")
proxy_client = importlib.util.module_from_spec(client_spec)
client_spec.loader.exec_module(proxy_client)
recovery_spec = importlib.util.spec_from_file_location("db_recovery", ROOT / "deploy/db-recovery.py")
recovery = importlib.util.module_from_spec(recovery_spec)
recovery_spec.loader.exec_module(recovery)


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
        "FORWARDED_ALLOW_IPS": "192.0.2.10",
        "POSTGRES_USER": "check",
        "POSTGRES_PASSWORD": "synthetic-only-password",
        "POSTGRES_DB": "check",
        "DATABASE_URL": "postgresql://check:synthetic-only-password@db:5432/check",
    }
    for index, name in enumerate(("AUTH_ACCESS_KEY", "AUTH_REFRESH_KEY", "AUTH_REFRESH_DIGEST_KEY", "AUTH_CSRF_KEY")):
        values[name] = base64.b64encode(bytes([index + 1]) * 32).decode()
    return values


class ProductionGuardChecks(unittest.TestCase):
    def test_conflicting_proxy_environments_stop_preflight_and_exec(self):
        for override in ("*", "127.0.0.1", "192.0.2.11", ""):
            for command in (["--validate-only"], ["uvicorn", "app.main:app"]):
                with (
                    self.subTest(override=override, command=command),
                    patch.dict(
                        entrypoint.os.environ,
                        {**synthetic_values(), "UVICORN_FORWARDED_ALLOW_IPS": override},
                        clear=True,
                    ),
                    patch.object(entrypoint.sys, "argv", ["backend-entrypoint.py", *command]),
                    patch.object(entrypoint.os, "execvp") as execute,
                    patch("builtins.print") as output,
                ):
                    self.assertEqual(entrypoint.main(), 2)
                    execute.assert_not_called()
                    self.assertEqual(output.call_args.args, ("Production configuration rejected (values hidden).",))

    def test_matching_proxy_environments_reach_uvicorn_unchanged(self):
        from click.testing import CliRunner
        from uvicorn import Config
        from uvicorn.main import main as uvicorn_cli

        command = json.loads(
            next(
                line[4:]
                for line in (ROOT / "deploy/backend.Dockerfile").read_text().splitlines()
                if line.startswith("CMD ")
            )
        )
        for trusted in ("192.0.2.10", "192.0.2.0/28, 2001:db8::/64"):
            for prefixed in (False, True):
                values = {**synthetic_values(), "FORWARDED_ALLOW_IPS": trusted}
                if prefixed:
                    values["UVICORN_FORWARDED_ALLOW_IPS"] = trusted
                with (
                    self.subTest(trusted=trusted, prefixed=prefixed),
                    patch.dict(entrypoint.os.environ, values, clear=True),
                ):
                    for arguments in (["--validate-only"], command):
                        with (
                            patch.object(entrypoint.sys, "argv", ["backend-entrypoint.py", *arguments]),
                            patch.object(entrypoint.os, "execvp") as execute,
                            patch("builtins.print"),
                        ):
                            if arguments == ["--validate-only"]:
                                self.assertEqual(entrypoint.main(), 0)
                                execute.assert_not_called()
                            else:
                                entrypoint.main()
                                execute.assert_called_once_with(command[0], command)
                    with patch("uvicorn.main.run") as run:
                        result = CliRunner().invoke(uvicorn_cli, command[1:])
                        self.assertEqual(result.exit_code, 0, result.output)
                    options = dict(run.call_args.kwargs)
                    options.pop("app_dir")
                    config = Config(*run.call_args.args, **options)
                    self.assertEqual(config.forwarded_allow_ips, trusted)
                    self.assertTrue(config.proxy_headers)
                    self.assertEqual(config.workers, 1)

    def test_matching_invalid_proxy_environments_are_still_rejected(self):
        for trusted in ("", "*", "0.0.0.0/0", "::/0", "nginx"):
            with (
                self.subTest(trusted=trusted),
                self.assertRaisesRegex(ValueError, "^explicit_proxy_addresses_required$"),
            ):
                entrypoint.validate(
                    {**synthetic_values(), "FORWARDED_ALLOW_IPS": trusted, "UVICORN_FORWARDED_ALLOW_IPS": trusted}
                )

    def test_proxy_addresses_require_explicit_bounded_ips(self):
        for address in ("192.0.2.10", "2001:db8::1", "192.0.2.0/28, 2001:db8::/64"):
            with self.subTest(address=address):
                entrypoint.validate({**synthetic_values(), "FORWARDED_ALLOW_IPS": address})
        invalid = (
            "",
            " ",
            "*",
            "0.0.0.0/0",
            "::/0",
            "nginx",
            "192.0.2.10:8000",
            "http://192.0.2.10",
            "/tmp/nginx.sock",
            "192.0.2.10,",
            "192.0.2.10,,::1",
            "192.0.2.10/24",
            "fe80::1%eth0",
            "fe80::%eth0/64",
            "not-an-ip",
        )
        values = synthetic_values()
        del values["FORWARDED_ALLOW_IPS"]
        with self.assertRaisesRegex(ValueError, "^explicit_proxy_addresses_required$"):
            entrypoint.validate(values)
        for address in invalid:
            with self.subTest(address=address):
                with self.assertRaisesRegex(ValueError, "^explicit_proxy_addresses_required$"):
                    entrypoint.validate({**synthetic_values(), "FORWARDED_ALLOW_IPS": address})

    def test_proxy_validation_precedes_exec_and_does_not_print_values(self):
        for command in (["--validate-only"], ["uvicorn", "app.main:app"]):
            with (
                patch.dict(
                    entrypoint.os.environ,
                    {**synthetic_values(), "FORWARDED_ALLOW_IPS": "secret-canary"},
                    clear=True,
                ),
                patch.object(entrypoint.sys, "argv", ["backend-entrypoint.py", *command]),
                patch.object(entrypoint.os, "execvp") as execute,
                patch("builtins.print") as output,
            ):
                self.assertEqual(entrypoint.main(), 2)
                execute.assert_not_called()
                self.assertEqual(
                    output.call_args.args,
                    ("Production configuration rejected (values hidden).",),
                )

    def test_valid_proxy_environment_reaches_preflight_and_existing_exec(self):
        for command in (
            ["--validate-only"],
            ["uvicorn", "app.main:app"],
            ["python", "-c", "pass"],
        ):
            values = synthetic_values()
            values["FORWARDED_ALLOW_IPS"] = "192.0.2.10, 2001:db8::/64"
            with (
                patch.dict(entrypoint.os.environ, values, clear=True),
                patch.object(entrypoint.sys, "argv", ["backend-entrypoint.py", *command]),
                patch.object(entrypoint.os, "execvp") as execute,
            ):
                if command == ["--validate-only"]:
                    self.assertEqual(entrypoint.main(), 0)
                    execute.assert_not_called()
                else:
                    entrypoint.main()
                    execute.assert_called_once_with(command[0], command)
                    self.assertEqual(
                        entrypoint.os.environ["FORWARDED_ALLOW_IPS"],
                        values["FORWARDED_ALLOW_IPS"],
                    )

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
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", nginx)
        self.assertIn('proxy_set_header Forwarded "";', nginx)
        self.assertNotIn("$proxy_add_x_forwarded_for", nginx)
        command = json.loads(
            next(
                line[4:]
                for line in (ROOT / "deploy/backend.Dockerfile").read_text().splitlines()
                if line.startswith("CMD ")
            )
        )
        self.assertEqual(command[command.index("--workers") + 1], "1")
        self.assertIn("--proxy-headers", command)
        self.assertIn("--no-access-log", command)
        self.assertNotIn("--no-proxy-headers", command)
        self.assertNotIn("--forwarded-allow-ips", command)
        self.assertNotIn("--reload", command)
        self.assertIn("error_log /dev/null;", nginx)
        self.assertIn("proxy_buffering off;", nginx)
        self.assertIn("proxy_read_timeout 75s;", nginx)
        log_line = next(line for line in nginx.splitlines() if "log_format safe" in line)
        for forbidden in ("$request_uri", "$args", "$http_cookie", "$http_authorization", "$request "):
            self.assertNotIn(forbidden, log_line)


class SmokeCompletionChecks(unittest.TestCase):
    """Offline acceptance probes: failed/nonterminal/wrong answers cannot PASS."""

    def test_proxy_probe_preserves_duplicate_headers_without_exporting_credentials(self):
        task = {
            "body": {"email": "fixture@example.test", "password": "synthetic-private-password"},
            "headers": [["X-Forwarded-For", ""], ["X-Forwarded-For", "invalid-canary"]],
        }
        response = Mock(status=200)
        response.read.return_value = b'{"access_token":"synthetic-private-token"}'
        response.getheaders.return_value = [("Set-Cookie", "refresh_token=synthetic-private-cookie; Secure; HttpOnly")]
        response.getheader.return_value = None
        connection = Mock()
        connection.getresponse.return_value = response
        output = StringIO()
        with (
            patch.object(proxy_client.http.client, "HTTPSConnection", return_value=connection),
            patch.object(proxy_client.sys, "stdin", StringIO(json.dumps({"tasks": [task]}))),
            patch.object(proxy_client.sys, "stdout", output),
        ):
            proxy_client.main()
        connection.putheader.assert_any_call("X-Forwarded-For", "")
        connection.putheader.assert_any_call("X-Forwarded-For", "invalid-canary")
        connection.close.assert_called_once()
        result = json.loads(output.getvalue())[0]
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["secure_cookies"] and result["access_token_present"])
        self.assertFalse(result["password_echo"])
        self.assertNotIn("synthetic-private", output.getvalue())
        self.assertNotIn("fixture@example.test", output.getvalue())

    def test_smoke_trust_is_single_nginx_on_its_exact_project_network(self):
        network = {
            "Name": "test-project_proxy",
            "Labels": {"com.docker.compose.project": "test-project"},
            "IPAM": {"Config": [{"Subnet": "172.28.74.0/29"}]},
        }
        self.assertEqual(smoke.smoke_proxy_address(network, "test-project"), "172.28.74.2")
        entrypoint.validate(
            {
                **synthetic_values(),
                "FORWARDED_ALLOW_IPS": smoke.smoke_proxy_address(network, "test-project"),
            }
        )
        for change in (
            {"Name": "existing_default"},
            {"Labels": {}},
            {"IPAM": {"Config": []}},
            {"IPAM": {"Config": [{"Subnet": "0.0.0.0/0"}]}},
            {"IPAM": {"Config": [{"Subnet": "not-a-subnet"}]}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                smoke.smoke_proxy_address({**network, **change}, "test-project")

    def test_smoke_fails_on_subnet_overlap_without_claiming_other_resources(self):
        def network(subnet):
            return {"IPAM": {"Config": [{"Subnet": subnet}]}}

        smoke.reject_proxy_overlap([network("172.31.32.0/20"), network("172.17.0.0/16"), network("2001:db8::/64")])
        for subnet in ("172.28.74.0/29", "172.28.0.0/16", "172.28.74.0/30"):
            with self.subTest(subnet=subnet), self.assertRaisesRegex(ValueError, "proxy_subnet_already_in_use"):
                smoke.reject_proxy_overlap([network(subnet)])

    def test_smoke_requires_fixed_proxy_and_separate_database_with_egress(self):
        config = {
            "services": {
                "nginx": {"networks": {"proxy": {"ipv4_address": "172.28.74.2"}}},
                "backend": {
                    "networks": {"proxy": {}, "database": {}},
                    "environment": {"FORWARDED_ALLOW_IPS": "172.28.74.2"},
                },
                "db": {"networks": {"database": {}}},
            },
            "networks": {
                "proxy": {
                    "ipam": {
                        "config": [{"subnet": "172.28.74.0/29", "ip_range": "172.28.74.4/30", "gateway": "172.28.74.1"}]
                    }
                },
                "database": {"internal": True},
            },
        }
        smoke.validate_proxy_layout(config)
        for change in (
            "nginx_database",
            "wrong_proxy",
            "backend_no_egress",
            "db_proxy",
            "internal_proxy",
            "broad_trust",
            "dynamic_pool",
        ):
            bad = deepcopy(config)
            if change == "nginx_database":
                bad["services"]["nginx"]["networks"]["database"] = {}
            elif change == "wrong_proxy":
                bad["services"]["nginx"]["networks"]["proxy"]["ipv4_address"] = "172.28.74.3"
            elif change == "backend_no_egress":
                del bad["services"]["backend"]["networks"]["proxy"]
            elif change == "db_proxy":
                bad["services"]["db"]["networks"]["proxy"] = {}
            elif change == "internal_proxy":
                bad["networks"]["proxy"]["internal"] = True
            elif change == "broad_trust":
                bad["services"]["backend"]["environment"]["FORWARDED_ALLOW_IPS"] = "172.28.74.0/29"
            else:
                del bad["networks"]["proxy"]["ipam"]["config"][0]["ip_range"]
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "dedicated_proxy_database_layout"):
                smoke.validate_proxy_layout(bad)

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


def recovery_snapshot(**changes) -> dict:
    """A synthetic snapshot with every product table and no reservation FK yet."""
    value = {
        "server_version_num": "170011",
        "tables": {name: {"rows": 2, "sha256": "0" * 64} for name in recovery.TABLES},
        "groups": {"chat_jobs": {"queued": 1}, "messages": {"user:completed": 2}, "chat_quota_reservations": {}},
        "orphan_job_reservations": 0,
        "constraints": ["chat_jobs_pkey", "messages_pkey"],
        "indexes": ["chat_jobs_pkey", "messages_pkey"],
        "database_bytes": 1 << 20,
        "other_tables": [],
    }
    value.update(changes)
    return value


class FakeDatabase(recovery.Database):
    """Records every call; never runs Docker or SQL."""

    def __init__(self, snapshots, *, running=False, existing=(), restored=None, free=1 << 40):
        super().__init__([], runner=self.unexpected_runner)
        self.snapshots, self.running, self.existing = list(snapshots), running, set(existing)
        self.restored, self.free = restored, free
        self.calls = []

    @staticmethod
    def unexpected_runner(*args, **kwargs):
        raise AssertionError("no command may run")

    def snapshot(self, database=recovery.MAIN_DB):
        self.calls.append(("snapshot", database))
        if database != recovery.MAIN_DB and self.restored is not None:
            return deepcopy(self.restored)
        return deepcopy(self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0])

    def sql(self, statements, *, code, database=recovery.MAIN_DB, variables=""):
        self.calls.append(("sql", code, database))
        return ""

    def backend_running(self):
        return self.running

    def free_bytes(self):
        return self.free

    def database_exists(self, name):
        return name in self.existing

    def dump(self, path):
        self.calls.append(("dump",))
        path.write_bytes(b"synthetic archive")

    def archive_list(self, path):
        return "\n".join(f"1; 0 0 TABLE DATA public {name} owner" for name in recovery.TABLES)

    def restore_into(self, name, path):
        self.calls.append(("restore_into", name))

    def create(self, name):
        self.calls.append(("create", name))

    def drop(self, name):
        self.calls.append(("drop", name))

    def swap(self, retained):
        self.calls.append(("swap", retained))


class DataRecoveryChecks(unittest.TestCase):
    """#75 gates: stopped backend, fresh matching backup, no orphans, no live-DB drop, no leaked values."""

    def setUp(self):
        import tempfile
        from datetime import datetime, timezone

        self.directory = tempfile.TemporaryDirectory(prefix="recovery-check-")
        self.work = Path(self.directory.name)
        self.now = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.directory.cleanup()

    def manifest(self, *, purpose="pre-change", snapshot=None, age_minutes=5, name="backup-x", database_bytes=1 << 20):
        from datetime import timedelta

        archive = self.work / (name + ".dump")
        archive.write_bytes(b"synthetic archive " + name.encode())
        manifest = {
            "schema_version": recovery.MANIFEST_VERSION,
            "database_bytes": database_bytes,
            "file": archive.name,
            "bytes": archive.stat().st_size,
            "sha256": recovery.file_sha256(archive),
            "created_at": (self.now - timedelta(minutes=age_minutes)).isoformat(),
            "purpose": purpose,
            "backend_running": False,
            "consistent": snapshot is not False,
            "snapshot": recovery_snapshot() if snapshot is None else (snapshot or None),
        }
        path = self.work / (name + ".manifest.json")
        path.write_text(json.dumps(manifest))
        return path

    def test_differences_name_changed_tables_and_unexpected_constraints(self):
        before = recovery_snapshot()
        self.assertEqual(recovery.differences(before, deepcopy(before)), [])
        after = deepcopy(before)
        after["tables"]["messages"]["sha256"] = "1" * 64
        after["database_bytes"] = 5
        self.assertEqual(recovery.differences(before, after), ["tables.messages"])
        with_fk = recovery_snapshot(constraints=before["constraints"] + [recovery.FK_NAME])
        self.assertEqual(recovery.differences(before, with_fk, added=frozenset({recovery.FK_NAME})), [])
        self.assertEqual(recovery.differences(before, with_fk), ["constraints"])
        self.assertEqual(recovery.differences(with_fk, before, removed=frozenset({recovery.FK_NAME})), [])

    def test_classification_separates_empty_init_partial_and_existing_apply(self):
        self.assertEqual(recovery.classify(recovery_snapshot(tables={})), "empty_use_db_init")
        self.assertEqual(recovery.classify(recovery_snapshot(tables={"users": {}})), "partial_stop")
        self.assertEqual(recovery.classify(recovery_snapshot()), "needs_reservation_fk")
        current = recovery_snapshot(constraints=[recovery.FK_NAME])
        self.assertEqual(recovery.classify(current), "current")

    def test_snapshot_query_is_read_only_and_limited_to_present_tables(self):
        sql = recovery.snapshot_sql({"users", "messages"})
        self.assertIn("BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ;", sql)
        for forbidden in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE", "TRUNCATE"):
            self.assertNotIn(forbidden, sql.upper().replace("CURRENT_SETTING", ""))
        self.assertIn("FROM users AS t", sql)
        self.assertNotIn("FROM chat_jobs AS t", sql)
        self.assertIn("'orphan_job_reservations', NULL", sql)

    def test_manifest_must_name_an_unchanged_archive_in_its_own_directory(self):
        path = self.manifest()
        recovery.load_manifest(path)
        data = json.loads(path.read_text())
        for change, code in (
            ({"file": "../outside.dump"}, "manifest_invalid"),
            ({"purpose": "weekly"}, "manifest_invalid"),
            ({"extra": 1}, "manifest_invalid"),
            ({"bytes": data["bytes"] + 1}, "backup_size_mismatch"),
            ({"sha256": "f" * 64}, "backup_sha256_mismatch"),
        ):
            with self.subTest(change=change):
                path.write_text(json.dumps({**data, **change}))
                with self.assertRaisesRegex(recovery.Failure, f"^{code}$"):
                    recovery.load_manifest(path)

    def test_apply_requires_stopped_backend_fresh_matching_pre_change_backup_and_no_orphans(self):
        cases = (
            ({"running": True}, {}, "stop_backend_first"),
            ({}, {"age_minutes": 61}, "fresh_pre_change_backup_required"),
            ({}, {"age_minutes": -5}, "fresh_pre_change_backup_required"),
            ({}, {"purpose": "daily"}, "fresh_pre_change_backup_required"),
            ({}, {"snapshot": False}, "backup_does_not_match_current"),
            ({}, {"snapshot": recovery_snapshot(orphan_job_reservations=3)}, "backup_does_not_match_current"),
        )
        for options, manifest, code in cases:
            with self.subTest(code=code, manifest=manifest):
                db = FakeDatabase([recovery_snapshot()], **options)
                path = self.manifest(**manifest, name=f"backup-{len(manifest)}-{code}")
                with self.assertRaisesRegex(recovery.Failure, f"^{code}$"):
                    recovery.apply_fk(db, self.work, path, self.now)
                self.assertNotIn(("sql", "apply_failed", recovery.MAIN_DB), db.calls)
        orphaned = recovery_snapshot(orphan_job_reservations=1)
        db = FakeDatabase([orphaned])
        with self.assertRaisesRegex(recovery.Failure, "^orphan_reservations$"):
            recovery.apply_fk(db, self.work, self.manifest(snapshot=orphaned, name="backup-orphan"), self.now)
        self.assertNotIn(("sql", "apply_failed", recovery.MAIN_DB), db.calls)

    def test_apply_runs_one_transaction_then_proves_only_the_fk_changed(self):
        before = recovery_snapshot()
        after = recovery_snapshot(constraints=before["constraints"] + [recovery.FK_NAME])
        db = FakeDatabase([before, before, after])
        result = recovery.apply_fk(db, self.work, self.manifest(), self.now)
        self.assertEqual(result["result"], "applied")
        self.assertEqual([call for call in db.calls if call[0] == "sql"], [("sql", "apply_failed", recovery.MAIN_DB)])
        self.assertIn("ADD CONSTRAINT chat_jobs_reservation_fk", recovery.APPLY_FK)
        self.assertLess(recovery.APPLY_FK.index("BEGIN;"), recovery.APPLY_FK.index("ALTER TABLE"))
        self.assertNotIn("DROP", recovery.APPLY_FK)
        damaged = deepcopy(after)
        damaged["tables"]["chat_jobs"]["rows"] = 1
        with self.assertRaisesRegex(recovery.Failure, "^unexpected_change_after_apply$"):
            later = self.now.replace(second=1)
            recovery.apply_fk(FakeDatabase([before, before, damaged]), self.work, self.manifest(name="b2"), later)
        current = FakeDatabase([after])
        self.assertEqual(recovery.apply_fk(current, self.work, Path("unused"), self.now)["result"], "already_applied")
        self.assertNotIn("TABLE", recovery.REVERT_FK.replace("ALTER TABLE chat_jobs", ""))

    def test_backup_refuses_running_backend_for_pre_change_and_flags_concurrent_writes(self):
        db = FakeDatabase([recovery_snapshot()], running=True)
        with self.assertRaisesRegex(recovery.Failure, "^stop_backend_first$"):
            recovery.backup(db, self.work, "pre-change", self.now)
        self.assertNotIn(("dump",), db.calls)
        changed = recovery_snapshot(tables={**recovery_snapshot()["tables"], "messages": {"rows": 3, "sha256": "1"}})
        result = recovery.backup(
            FakeDatabase([recovery_snapshot(), changed], running=True), self.work, "daily", self.now
        )
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertFalse(manifest["consistent"])
        self.assertIsNone(manifest["snapshot"])

        def partial_dump(path):
            path.write_bytes(b"partial")
            raise recovery.Failure("backup_dump_failed")

        failing = FakeDatabase([recovery_snapshot()])
        failing.dump = partial_dump
        with self.assertRaisesRegex(recovery.Failure, "^backup_dump_failed$"):
            recovery.backup(failing, self.work, "pre-change", self.now.replace(minute=1))
        self.assertEqual(len(list(self.work.glob("*.dump"))), 1)  # Only the earlier complete archive remains.

    def test_restore_space_follows_recorded_database_size_not_archive_size(self):
        database_bytes = 3 << 30  # A 3 GiB database behind a tiny compressed archive.
        needed = database_bytes * 2 + recovery.RESTORE_HEADROOM
        expected = recovery_snapshot()
        path = self.manifest(database_bytes=database_bytes, name="space")

        def run(command, free, second):
            db = FakeDatabase([expected], restored=expected, free=free)
            moment = self.now.replace(second=second)
            if command == "verify":
                recovery.verify(db, self.work, path, True, moment)
            else:
                recovery.restore(db, self.work, path, None, moment)
            return db

        for second, command in enumerate(("verify", "restore")):
            with self.subTest(command=command):
                with self.assertRaisesRegex(recovery.Failure, "^insufficient_volume_space$"):
                    run(command, needed - 1, second)
                db = run(command, needed, second + 10)
                staging = recovery.CHECK_DB if command == "verify" else recovery.STAGING_DB
                self.assertIn(("create", staging), db.calls)
        data = json.loads(path.read_text())
        for value in (None, 0, -1, "1024", True):
            with self.subTest(database_bytes=value):
                path.write_text(json.dumps({**data, "database_bytes": value}))
                with self.assertRaisesRegex(recovery.Failure, "^manifest_invalid$"):
                    recovery.load_manifest(path)

    def test_backup_manifest_records_the_larger_database_size(self):
        before, after = recovery_snapshot(database_bytes=100), recovery_snapshot(database_bytes=300)
        result = recovery.backup(FakeDatabase([before, after]), self.work, "pre-change", self.now)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["database_bytes"], 300)
        self.assertEqual(manifest["schema_version"], recovery.MANIFEST_VERSION)
        recovery.load_manifest(Path(result["manifest"]))

    def test_restore_stages_checks_and_swaps_without_dropping_the_live_database(self):
        expected = recovery_snapshot()
        db = FakeDatabase([recovery_snapshot(), expected], restored=expected)
        result = recovery.restore(db, self.work, self.manifest(), None, self.now)
        self.assertEqual(result["retained_database"], "nss_replaced_20261107t030000z")
        self.assertEqual(
            [call for call in db.calls if call[0] in {"create", "restore_into", "swap", "drop"}],
            [
                ("create", recovery.STAGING_DB),
                ("restore_into", recovery.STAGING_DB),
                ("swap", result["retained_database"]),
            ],
        )
        mismatch = FakeDatabase([recovery_snapshot()], restored=recovery_snapshot(orphan_job_reservations=9))
        with self.assertRaisesRegex(recovery.Failure, "^restored_snapshot_mismatch$"):
            recovery.restore(mismatch, self.work, self.manifest(name="b2"), None, self.now)
        self.assertIn(("drop", recovery.STAGING_DB), mismatch.calls)
        self.assertFalse(any(call[0] == "swap" for call in mismatch.calls))
        for options, manifest, code in (
            ({"running": True}, {}, "stop_backend_first"),
            ({}, {"snapshot": False}, "expected_snapshot_required"),
            ({"existing": {recovery.STAGING_DB}}, {}, "staging_database_exists"),
        ):
            with self.subTest(code=code):
                db = FakeDatabase([expected], restored=expected, **options)
                with self.assertRaisesRegex(recovery.Failure, f"^{code}$"):
                    recovery.restore(db, self.work, self.manifest(**manifest, name="b-" + code), None, self.now)
        swap_sql = []
        database = recovery.Database([], runner=lambda argv, **kwargs: swap_sql.append(kwargs["input_bytes"]) or b"")
        database.swap("nss_replaced_x")
        self.assertIn(b'ALTER DATABASE :"target" RENAME TO nss_replaced_x', swap_sql[0])
        self.assertNotIn(b"DROP", swap_sql[0])

    def test_failed_commands_keep_server_text_private_and_print_only_codes(self):
        secret = "synthetic-key-value-canary"
        command = [sys.executable, "-c", f"import sys; sys.stderr.write('{secret}'); sys.exit(3)"]
        with self.assertRaises(recovery.Failure) as raised:
            recovery.run_command(command, code="apply_failed", diagnostics=self.work)
        self.assertEqual(str(raised.exception), "apply_failed")
        self.assertIn(secret, next(self.work.glob("diagnostics-*-apply_failed.txt")).read_text())
        for argv in (["--db-container", "x", "--work-dir", str(ROOT / "tmp-recovery"), "precheck"],):
            error = StringIO()
            with patch.object(recovery.sys, "stderr", error):
                self.assertEqual(recovery.main(argv), 1)
            self.assertEqual(error.getvalue(), "DB_RECOVERY_FAILED: work_dir_inside_repository\n")
            self.assertFalse((ROOT / "tmp-recovery").exists())

    def test_compose_target_passes_only_paths_and_checks_backend_state(self):
        args = recovery.parser().parse_args(
            [
                "--compose-project",
                "next-stop-siren",
                "--compose-file",
                "/opt/next-stop-siren/releases/r/compose.yaml",
                "--env-file",
                "/etc/next-stop-siren/production.env",
                "--work-dir",
                "/var/backups/next-stop-siren",
                "precheck",
            ]
        )
        db = recovery.build_database(args, self.work)
        self.assertEqual(db.exec_prefix[-3:], ["exec", "-T", "db"])
        self.assertEqual(db.backend_check[-5:], ["ps", "--status", "running", "--quiet", "backend"])
        self.assertIn(str(Path("/etc/next-stop-siren/production.env")), db.exec_prefix)
        self.assertNotIn("POSTGRES_PASSWORD", " ".join(db.exec_prefix))
        for template in (recovery.PSQL, recovery.PG_DUMP, recovery.PG_RESTORE):
            self.assertIn('"$POSTGRES_USER"', template)

    def test_container_logs_rotate_with_a_fixed_disk_ceiling(self):
        compose = (ROOT / "deploy/compose.yaml").read_text()
        self.assertEqual(compose.count("logging: &logging"), 1)
        self.assertEqual(compose.count("logging: *logging"), 2)
        self.assertIn('driver: json-file\n      options:\n        max-size: "5m"\n        max-file: "2"', compose)
        # Three services x 2 files x 5 MiB: at most 30 MiB of container logs on the host.
        self.assertEqual(compose.count("\n    logging:"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
