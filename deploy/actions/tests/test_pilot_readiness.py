"""Actual child-main/HTTPS fixtures; Docker is a standalone synthetic CLI, never a live stack."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import runpy
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "verify-pilot-readiness"
HOST = "next-stop-siren.duckdns.org"
SOURCE = "a" * 40
FRONTEND = "ghcr.io/next-stop-siren/frontend@sha256:" + "d" * 64
BACKEND = "ghcr.io/next-stop-siren/backend@sha256:" + "e" * 64
MARKER = "READINESS_SECRET_CANARY"
# Test harness only. The installed product accepts no test transport/config overrides.
ACTUAL_CONTAINER = os.environ.get("PILOT_TEST_CONTAINER") == "1"
if ACTUAL_CONTAINER and not (
    Path("/.dockerenv").is_file()
    and SCRIPT == Path("/probe/actions/verify-pilot-readiness")
    and Path("/probe/tls/good.crt").is_file()
):
    raise RuntimeError("Exact-path fixture requires the isolated owned container")


def setUpModule():
    global certificates, certificate_root
    certificate_root = tempfile.TemporaryDirectory(prefix="readiness-cert-")
    certificates = Path(certificate_root.name)
    for name, hostname in (("good", HOST), ("bad", "other.invalid")):
        if ACTUAL_CONTAINER:
            for extension in ("crt", "key"):
                shutil.copyfile(Path("/probe/tls") / (name + "." + extension), certificates / (name + "." + extension))
        else:
            command = [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=" + hostname, "-addext", "subjectAltName=DNS:" + hostname,
                "-keyout", str(certificates / (name + ".key")), "-out", str(certificates / (name + ".crt")),
            ]
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        (certificates / (name + ".key")).chmod(0o600)


def tearDownModule():
    certificate_root.cleanup()


class ReadinessHTTPSFixtureTests(unittest.TestCase):
    def run_fixture(self, mode="success", *, argv=None, cold=False):
        paths, sni = [], []
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="readiness-fixture-") as temporary:
            root = Path(temporary)
            state = root / "state.json"
            state.write_text(json.dumps({"mode": mode, "changed": False}))
            fake = root / "docker"
            fake.write_text(
                "#!" + sys.executable + " -S\n" + '''import json,os,sys,time
from pathlib import Path
state=Path(STATE)
value=json.loads(state.read_text())
args=sys.argv[1:]
assert args[:2]==['--host','unix:///var/run/docker.sock']
args=args[2:]
with state.with_suffix('.commands').open('a') as log:log.write(json.dumps(args)+'\\n')
if value['mode']=='docker_stall':
    state.with_suffix('.pid').write_text(str(os.getpid()))
    time.sleep(10)
if value['mode']=='docker_large':print('READINESS_SECRET_CANARY'*2000);raise SystemExit(0)
if args[0]=='ps':
    assert args[:4]==['ps','--all','--no-trunc','--quiet']
    assert 'label=com.docker.compose.oneoff=False' in args
    assert 'label=com.docker.compose.project=next-stop-siren' in args
    service='nginx' if 'label=com.docker.compose.service=nginx' in args else 'backend'
    cid=('1' if service=='nginx' else '2')*64
    if value['changed'] and service=='nginx' and value['mode']!='changed_binding':cid='9'*64
    if value['mode']=='duplicate':cid+='\\n'+'8'*64
    print(cid)
elif args[0]=='inspect':
    assert args[1:4]==['--type','container','--format']
    assert '.Config.Env' not in args[4]
    cid=args[-1];service='backend' if cid=='2'*64 else 'nginx'
    image='sha256:'+('4' if service=='backend' else '3')*64
    if value['mode']=='wrong_image':image='sha256:'+'5'*64
    ports=[{'HostIp':'0.0.0.0','HostPort':'443'}] if service=='nginx' else None
    if service=='nginx':
        if value['mode']=='wrong_listener':ports=[{'HostIp':'0.0.0.0','HostPort':'8443'}]
        if value['mode']=='missing_listener':ports=None
        if value['mode']=='nonloopback_listener':ports=[{'HostIp':'192.0.2.20','HostPort':'443'}]
        if value['mode']=='ipv6_listener':ports=[{'HostIp':'::','HostPort':'443'}]
        if value['mode']=='malformed_listener':ports=[{'HostIp':'0.0.0.0','HostPort':443}]
        if value['mode']=='loopback_binding' or value['mode']=='changed_binding' and value['changed']:
            ports=[{'HostIp':'127.0.0.1','HostPort':'443'}]
    print(json.dumps({'id':cid,'image':image,'running':value['mode']!='stopped',
        'project':'foreign' if value['mode']=='foreign_project' else 'next-stop-siren',
        'service':service,'oneoff':'True' if value['mode']=='oneoff' else 'False','https':ports}))
elif args[:2]==['image','inspect']:
    assert args[2]=='--format' and '.Config.Env' not in args[3]
    image='sha256:'+('3' if '/frontend@' in args[-1] else '4')*64
    print(json.dumps({'id':image,'source':('f' if value['mode']=='wrong_source' else 'a')*40}))
else:raise SystemExit(9)
'''.replace("STATE", repr(str(state)))
            )
            fake.chmod(0o755)

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass

                def do_GET(self):
                    paths.append((self.command, self.path, self.headers.get("Host")))
                    try:
                        self.respond()
                    except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                        pass

                def respond(self):
                    path = self.path
                    status, kind = 200, "application/json"
                    body = b'{"status":"ready"}'
                    extra = {}
                    if path == "/api/ready":
                        if mode == "ready503" or cold and time.monotonic() - started < 4:
                            status, body = 503, b'{"code":"not_ready"}'
                        elif mode == "ready_shape":
                            body = b'{"status":"ok"}'
                        elif mode == "ready_duplicate":
                            body = b'{"status":"not_ready","status":"ready"}'
                        elif mode == "ready_html":
                            kind, body = "text/html", b"<html>fallback</html>"
                        elif mode in {"stalled_headers", "stalled_body"}:
                            if mode == "stalled_headers":
                                self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Wait: ")
                            else:
                                self.connection.sendall(
                                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 999\r\n\r\n"
                                )
                            for _ in range(30):
                                self.connection.sendall(b"x")
                                time.sleep(0.1)
                            return
                    elif path == "/api/me":
                        status = 200 if mode == "guest200" else 401
                        extra["WWW-Authenticate"] = "Bearer"
                        body = json.dumps({"error": {
                            "code": "wrong" if mode == "guest_wrong" else "unauthenticated",
                            "message": "A safe public localized message", "fields": {},
                            "trace_id": "12345678-1234-4234-8234-123456789abc",
                        }}).encode()
                    elif path == "/":
                        kind = "text/html"
                        asset = "/assets/index-abcdef12.js"
                        if mode == "asset_external":
                            asset = "https://" + MARKER + ".invalid/bundle.js"
                        elif mode == "asset_traversal":
                            asset = "/assets/../" + MARKER + ".js"
                        body = ('<!doctype html><html lang="ko"><div id="root"></div>'
                                '<script type="module" src="' + asset + '"></script></html>').encode()
                        if mode == "root_wrong":
                            body = b"<html>" + MARKER.encode() + b"</html>"
                        elif mode == "redirect":
                            status = 302
                            extra["Location"] = "https://" + MARKER + ".invalid/"
                        elif mode == "oversize":
                            body += b"x" * 140_000
                    elif path == "/assets/index-abcdef12.js":
                        kind, body = "application/javascript", b"console.log('fixture');"
                        if mode in {"asset_html", "asset_fallback"}:
                            body = b"<!doctype html><html>" + MARKER.encode() + b"</html>"
                            if mode == "asset_html":
                                kind = "text/html"
                        elif mode == "asset_empty":
                            body = b""
                        elif mode in {"changed", "changed_binding"}:
                            replacement = root / "replacement.json"
                            replacement.write_text(json.dumps({"mode": mode, "changed": True}))
                            os.replace(replacement, state)
                    else:
                        status, body = 404, b"not found"
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    for key, value in extra.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(body)

            server = ThreadingHTTPServer(("127.0.0.1", 443 if ACTUAL_CONTAINER else 0), Handler)
            server.daemon_threads = True
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            certificate = "bad" if mode == "wrong_hostname" else "good"
            context.load_cert_chain(certificates / (certificate + ".crt"), certificates / (certificate + ".key"))
            context.set_servername_callback(lambda sock, name, ctx: sni.append(name))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            ca = certificates / (("bad" if mode in {"wrong_hostname", "untrusted_tls"} else "good") + ".crt")
            total = 14 if cold else 10
            if ACTUAL_CONTAINER:
                target = Path("/usr/bin/docker")
                if target.is_symlink():
                    target.unlink()
                target.symlink_to(fake)
                shutil.copyfile(ca, "/etc/ssl/certs/ca-certificates.crt")
                command = [str(SCRIPT)]
            else:
                # Exact product functions/main execute in a new isolated Python process;
                # only test transport constants change in memory, never installed flags.
                bootstrap = root / "invoke.py"
                bootstrap.write_text(
                    "import runpy,sys\n"
                    "n=runpy.run_path(" + repr(str(SCRIPT)) + ",run_name='readiness_fixture')\n"
                    "g=n['main'].__globals__\n"
                    "g.update(" + repr({
                        "DOCKER": str(fake), "CAFILE": str(ca), "PORT": server.server_port,
                        "TOTAL_SECONDS": total, "OP_SECONDS": 0.75,
                        "BACKOFF_INITIAL": 1 if cold else 0.02, "BACKOFF_MAX": 3 if cold else 0.05,
                    }) + ")\nsys.exit(n['main']())\n"
                )
                command = [sys.executable, "-I", str(bootstrap)]
            arguments = argv if argv is not None else ["--source", SOURCE, "--frontend", FRONTEND, "--backend", BACKEND]
            malicious_modules = root / MARKER
            malicious_modules.mkdir()
            for module in ("ssl", "argparse", "json"):
                (malicious_modules / (module + ".py")).write_text("raise RuntimeError('" + MARKER + "')\n")
            environment = {**os.environ, "HTTP_PROXY": "http://" + MARKER + ".invalid:9",
                           "HTTPS_PROXY": "http://" + MARKER + ".invalid:9", "ALL_PROXY": "http://bad.invalid:9",
                           "SSL_CERT_FILE": str(certificates / "bad.crt"), "DOCKER_HOST": "tcp://bad.invalid:1234",
                           "PYTHONPATH": str(root / MARKER)}
            before = time.monotonic()
            try:
                result = subprocess.run(command + arguments, capture_output=True, text=True,
                                        env=environment, timeout=65 if ACTUAL_CONTAINER else total + 3)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            elapsed = time.monotonic() - before
            if mode == "docker_stall":
                self.assertTrue(state.with_suffix(".pid").exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(state.with_suffix(".pid").read_text()), 0)
            self.assertNotIn(MARKER, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertTrue(all(name == HOST for name in sni))
            self.assertTrue(all(method == "GET" and host.startswith(HOST) for method, _, host in paths))
            calls = state.with_suffix(".commands")
            docker_calls = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            return result, paths, docker_calls, elapsed

    def test_success_real_tls_and_guest_contract_no_proxy(self):
        result, paths, commands, _ = self.run_fixture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "PILOT_READINESS_OK\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual([path for _, path, _ in paths], ["/api/ready", "/api/me", "/", "/assets/index-abcdef12.js"])
        self.assertEqual(len(commands), 12)

    def test_wrong_listener_rejected_despite_valid_tls_responder(self):
        # These modes change Docker metadata only; the independent TLS server still
        # serves the same otherwise-good ready/guest/shell/asset responses.
        for mode in ("wrong_listener", "missing_listener", "nonloopback_listener",
                     "ipv6_listener", "malformed_listener"):
            with self.subTest(mode=mode):
                result, paths, _, _ = self.run_fixture(mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(paths, [])
                self.assertIn("HTTPS_LISTENER_IDENTITY", result.stderr)

    def test_explicit_ipv4_loopback_binding_is_accepted(self):
        result, paths, _, _ = self.run_fixture("loopback_binding")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(paths), 4)
        self.assertEqual(result.stderr, "")

    def test_binding_changes_during_probe_are_rejected(self):
        result, _, _, _ = self.run_fixture("changed_binding")
        self.assertEqual(result.returncode, 1)
        self.assertIn("CONTAINER_CHANGED_DURING_CHECK", result.stderr)

    def test_wrong_release_and_source_rejected_before_http(self):
        for mode in ("wrong_image", "wrong_source", "duplicate", "stopped", "foreign_project", "oneoff"):
            with self.subTest(mode=mode):
                result, paths, _, _ = self.run_fixture(mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(paths, [])
                expected = {"wrong_image": "IMAGE_IDENTITY", "wrong_source": "IMAGE_IDENTITY",
                            "duplicate": "CONTAINER_SELECTION", "stopped": "CONTAINER_IDENTITY",
                            "foreign_project": "CONTAINER_IDENTITY", "oneoff": "CONTAINER_IDENTITY"}
                self.assertIn(expected[mode], result.stderr)

    def test_not_ready_and_malformed_success_rejected(self):
        for mode in ("ready503", "ready_shape", "ready_duplicate", "ready_html", "guest200", "guest_wrong"):
            with self.subTest(mode=mode):
                result, _, _, elapsed = self.run_fixture(mode)
                self.assertEqual(result.returncode, 1)
                self.assertLess(elapsed, 65 if ACTUAL_CONTAINER else 13)

    def test_malformed_ready200_reaches_contract_rejection(self):
        for mode, code in (("ready_shape", "READINESS_CONTRACT"), ("ready_duplicate", "JSON_CONTRACT")):
            result, paths, _, _ = self.run_fixture(mode)
            self.assertEqual(result.returncode, 1)
            self.assertEqual([path for _, path, _ in paths], ["/api/ready"])
            self.assertIn(code, result.stderr)

    def test_spa_redirect_wrong_shell_and_assets_rejected(self):
        for mode in ("redirect", "root_wrong", "asset_external", "asset_traversal",
                     "asset_html", "asset_fallback", "asset_empty", "oversize"):
            with self.subTest(mode=mode):
                result, paths, _, _ = self.run_fixture(mode)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(all(path in {"/api/ready", "/api/me", "/", "/assets/index-abcdef12.js"}
                                    for _, path, _ in paths))

    def test_tls_trust_and_hostname_required(self):
        for mode in ("untrusted_tls", "wrong_hostname"):
            with self.subTest(mode=mode):
                result, _, _, _ = self.run_fixture(mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn("TLS_IDENTITY", result.stderr)

    def test_running_container_change_during_http_rejected(self):
        result, _, _, _ = self.run_fixture("changed")
        self.assertEqual(result.returncode, 1)
        self.assertIn("CONTAINER_CHANGED_DURING_CHECK", result.stderr)

    @unittest.skipIf(ACTUAL_CONTAINER, "deadline constants stay production defaults in exact executable fixture")
    def test_slow_headers_body_and_cli_are_bounded(self):
        for mode in ("stalled_headers", "stalled_body", "docker_stall", "docker_large"):
            with self.subTest(mode=mode):
                result, _, _, elapsed = self.run_fixture(mode)
                self.assertEqual(result.returncode, 1)
                self.assertLess(elapsed, 13)

    @unittest.skipIf(ACTUAL_CONTAINER, "host child-main covers >3s cold startup with shorter overall fixture budget")
    def test_cold_start_has_more_than_three_seconds_grace(self):
        result, paths, _, elapsed = self.run_fixture(cold=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(elapsed, 4)
        self.assertGreaterEqual(sum(path == "/api/ready" for _, path, _ in paths), 4)

    def test_malicious_arguments_fail_without_http_or_docker(self):
        for argv in (["--source", MARKER], ["--source", SOURCE, "--frontend", FRONTEND, "--backend", MARKER],
                     ["--source", SOURCE, "--front", FRONTEND, "--backend", BACKEND],
                     ["--source", SOURCE, "--source", SOURCE, "--backend", BACKEND]):
            with self.subTest(argv=argv):
                result, paths, commands, _ = self.run_fixture(argv=argv)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(paths, [])
                self.assertEqual(commands, [])
                self.assertIn("ARGUMENTS", result.stderr)

    def test_exact_installed_shebang_entrypoint_rejects_raw_argument(self):
        # No wrapper/config override: execute the actual file with its shebang.
        result = subprocess.run([str(SCRIPT), "--unknown", MARKER], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "PILOT_READINESS_FAILED: ARGUMENTS\n")
        self.assertNotIn(MARKER, result.stdout + result.stderr)


class QueryLifecycleTests(unittest.TestCase):
    """Deterministic alarm boundaries; no actual process, signal, pipe or network."""

    def probe(self, boundary, *, race=False, reap_timeout=False):
        namespace = runpy.run_path(str(SCRIPT), run_name="readiness_query_fixture")
        query = namespace["query"]
        rejected = namespace["Rejected"]
        process = MagicMock()
        process.pid = 424242
        process.poll.return_value = 0 if boundary in {"success_cleanup", "unmask"} else None
        process.stdout.fileno.return_value = 7
        process.wait.return_value = 0
        if reap_timeout:
            process.wait.side_effect = subprocess.TimeoutExpired(MARKER, 1)
        if boundary == "success_cleanup":
            process.wait.side_effect = [0, subprocess.TimeoutExpired(MARKER, 1)]
        mask = MagicMock(return_value=set())
        if boundary == "unmask":
            def deferred_alarm(how, signals):
                if how == signal.SIG_SETMASK:
                    self.assertTrue(process.stdout.close.called)
                    self.assertTrue(process.wait.called)
                    raise rejected("DEADLINE")
                return set()
            mask.side_effect = deferred_alarm
        fake_signal = SimpleNamespace(pthread_sigmask=mask, SIG_BLOCK=signal.SIG_BLOCK,
                                      SIG_SETMASK=signal.SIG_SETMASK, SIGALRM=signal.SIGALRM,
                                      SIGKILL=signal.SIGKILL)
        clock = MagicMock(return_value=0)
        if boundary == "setup":
            clock.side_effect = rejected("DEADLINE")
        ready = MagicMock(return_value=([process.stdout], [], []))
        if boundary == "read":
            ready.side_effect = rejected("DEADLINE")
        kill = MagicMock(side_effect=ProcessLookupError() if race else None)
        popen = MagicMock(return_value=process)
        fake_subprocess = SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE)
        with patch.dict(query.__globals__, {
            "subprocess": fake_subprocess, "signal": fake_signal,
            "time": SimpleNamespace(monotonic=clock), "select": SimpleNamespace(select=ready),
            "os": SimpleNamespace(killpg=kill, read=MagicMock(return_value=b"")),
        }):
            with self.assertRaises(rejected) as raised:
                query(["ps"], 10)
        self.assertEqual(str(raised.exception), "DOCKER_CLEANUP" if boundary == "success_cleanup" else "DEADLINE")
        self.assertNotIn(MARKER, str(raised.exception))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(mask.call_count, 2)
        process.stdout.close.assert_called_once()
        self.assertTrue(process.poll.called)
        self.assertEqual(process.wait.call_args.kwargs["timeout"], 1)
        if boundary not in {"success_cleanup", "unmask"}:
            kill.assert_called_once_with(process.pid, signal.SIGKILL)
        else:
            kill.assert_not_called()

    def test_deadline_after_spawn_before_initial_setup_cleans_owned_child(self):
        self.probe("setup")

    def test_pending_alarm_delivered_only_after_owned_cleanup(self):
        self.probe("unmask")

    def test_deadline_during_read_handles_exit_race(self):
        self.probe("read", race=True)

    def test_bounded_cleanup_does_not_replace_primary_deadline(self):
        self.probe("read", reap_timeout=True)

    def test_cleanup_failure_never_turns_into_success(self):
        self.probe("success_cleanup")


if __name__ == "__main__":
    unittest.main()
