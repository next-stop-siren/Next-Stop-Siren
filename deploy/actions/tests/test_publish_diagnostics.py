"""Offline publish CLI diagnostics with synthetic secret injection."""

import gc
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import warnings
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
from github_release import GitHub
from test_actions import context, GitHubFake, SHA


def setUpModule():
    global guards
    guards = [
        patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")),
        patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")),
        patch("subprocess.run", side_effect=AssertionError("unmocked process forbidden")),
        patch("subprocess.Popen", side_effect=AssertionError("unmocked process forbidden")),
    ]
    for guard in guards:
        guard.start()


def tearDownModule():
    for guard in reversed(guards):
        guard.stop()


class PublishDiagnosticTests(unittest.TestCase):
    """Exercise main -> publish -> real command with entirely offline boundaries."""

    marker = "SECRET_MARKER_must_never_appear"

    def run_cli(self, *, stage="registry_login", failure="exit", signal="", github=None, stdout_signal=""):
        with tempfile.TemporaryDirectory() as temporary:
            values = {
                **context(), "SOURCE_SHA": SHA, "RUNNER_TEMP": temporary,
                "GITHUB_TOKEN": self.marker, "GITHUB_OUTPUT": str(Path(temporary) / "output"),
                "PILOT_FRONTEND_REPOSITORY": "ghcr.io/next-stop-siren/frontend",
                "PILOT_BACKEND_REPOSITORY": "ghcr.io/next-stop-siren/backend",
            }
            calls = []

            def run(arguments, **kwargs):
                calls.append(arguments)
                current = "checkout" if arguments[0] == "git" else "registry_login"
                if "--metadata-file" in arguments:
                    name = Path(arguments[arguments.index("--metadata-file") + 1]).stem
                    current = name + "_build_push"
                    if stage != current or failure == "metadata":
                        metadata = {"containerimage.digest": "sha256:" + "d" * 64}
                        if stage == current and failure == "metadata":
                            metadata = {"containerimage.digest": self.marker}
                        Path(arguments[arguments.index("--metadata-file") + 1]).write_text(json.dumps(metadata))
                if stage == current and failure != "metadata":
                    if failure == "timeout":
                        raise subprocess.TimeoutExpired(
                            [self.marker], 900, output=self.marker, stderr=self.marker
                        )
                    if failure == "oserror":
                        raise OSError(self.marker)
                    return subprocess.CompletedProcess(arguments, 1, stdout_signal + self.marker, signal + self.marker)
                return subprocess.CompletedProcess(arguments, 0, SHA if current == "checkout" else "", "")

            if stage == "artifact_directory":
                (Path(temporary) / "pilot-release").mkdir()
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict("os.environ", values, clear=True),
                patch.object(cli, "GitHub", return_value=github or GitHubFake()),
            ):
                with patch.object(sys, "argv", ["cli.py", "publish", "--execute"]):
                    with (
                        patch("subprocess.run", side_effect=run),
                        patch("sys.stdout", stdout), patch("sys.stderr", stderr),
                    ):
                        result = cli.main()
            output = stdout.getvalue() + stderr.getvalue()
            self.assertEqual(result, 1)
            self.assertNotIn(self.marker, output)
            self.assertIn("no blind resend", output)
            self.assertFalse((Path(temporary) / "pilot-release" / "release.json").exists())
            return output, calls

    def test_command_auth_access_unknown_and_timeout(self):
        for stage in ("checkout", "registry_login", "frontend_build_push", "backend_build_push"):
            for failure, signal, reason in (
                ("exit", "unauthorized: authentication required ", "AUTHENTICATION_REJECTED"),
                ("exit", "denied: permission_denied ", "REGISTRY_ACCESS_DENIED"),
                ("exit", "unrecognized diagnostic ", "COMMAND_FAILED_UNKNOWN_CAUSE"),
                ("timeout", "", "TIMEOUT_OUTCOME_UNKNOWN"),
                ("oserror", "", "COMMAND_LAUNCH_FAILED"),
            ):
                with self.subTest(stage=stage, failure=failure, reason=reason):
                    output, calls = self.run_cli(stage=stage, failure=failure, signal=signal)
                    expected = "COMMAND_FAILED_UNKNOWN_CAUSE" if stage == "checkout" and failure == "exit" else reason
                    self.assertIn("stage=" + stage + "; reason=" + expected, output)
                    if failure == "exit":
                        self.assertIn("; exit=1;", output)
                    self.assertEqual(len(calls), 1 + (stage != "checkout") + (stage == "frontend_build_push")
                                     + 2 * (stage == "backend_build_push"))

    def test_buildx_feature_and_dockerfile_failures_are_static_hints(self):
        for signal, reason in (
            ("ERROR: attestations are not supported for the docker driver ", "BUILDER_FEATURE_UNSUPPORTED"),
            ("failed to solve: process /bin/sh did not complete successfully: exit code: 1 ", "DOCKERFILE_STEP_FAILED"),
        ):
            for stage in ("frontend_build_push", "backend_build_push"):
                output, _ = self.run_cli(stage=stage, signal=signal)
                self.assertIn("stage=" + stage + "; reason=" + reason + "; exit=1;", output)

    def test_stdout_is_not_used_as_registry_diagnostic(self):
        output, _ = self.run_cli(stdout_signal="unauthorized: authentication required ")
        self.assertIn("reason=COMMAND_FAILED_UNKNOWN_CAUSE; exit=1;", output)

    def test_unknown_api_exception_remains_unknown_and_redacted(self):
        github = GitHubFake()
        def fail(path):
            raise RuntimeError(self.marker)
        github.get = fail
        output, calls = self.run_cli(github=github)
        self.assertIn("stage=provenance; reason=UNKNOWN_FAILURE", output)
        self.assertEqual(calls, [])

    def test_long_build_stderr_uses_final_bounded_hint(self):
        output, _ = self.run_cli(
            stage="backend_build_push",
            signal=self.marker + "build progress " * 2000 + "unauthorized: authentication required ",
        )
        self.assertIn("stage=backend_build_push; reason=AUTHENTICATION_REJECTED; exit=1;", output)

    def test_network_and_allowlisted_guard_reasons(self):
        for error, reason in (
            (urllib.error.URLError(self.marker), "GITHUB_TRANSPORT_FAILED"),
            (ValueError("same_sha_trusted_ci_success_required"), "SAME_SHA_TRUSTED_CI_SUCCESS_REQUIRED"),
            (ValueError(self.marker), "PROVENANCE_OR_CONTRACT_REJECTED"),
        ):
            github = GitHubFake()
            def fail(path):
                raise error
            github.get = fail
            output, calls = self.run_cli(github=github)
            self.assertIn("stage=provenance; reason=" + reason, output)
            self.assertEqual(calls, [])

    def test_noncommand_directory_and_metadata_failures(self):
        output, calls = self.run_cli(stage="artifact_directory")
        self.assertIn("stage=artifact_directory; reason=LOCAL_IO_FAILED", output)
        self.assertEqual(len(calls), 1)
        for name in ("frontend", "backend"):
            output, _ = self.run_cli(stage=name + "_build_push", failure="metadata")
            self.assertIn("stage=" + name + "_metadata; reason=PROVENANCE_OR_CONTRACT_REJECTED", output)

    def test_api_error_does_not_print_url_or_body(self):
        github = GitHubFake()
        github.get = lambda path: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                self.marker, 403, self.marker, {"secret": self.marker}, io.BytesIO(self.marker.encode())
            )
        )
        output, calls = self.run_cli(github=github)
        self.assertIn("stage=provenance; reason=GITHUB_ACCESS_OR_INPUT", output)
        self.assertEqual(calls, [])

    def test_public_gate_keeps_static_actionable_reason(self):
        github = GitHubFake()
        github.visibility = "private"
        output, calls = self.run_cli(github=github)
        self.assertIn("stage=package_preflight; reason=OWNER_PUBLIC_PACKAGE_REQUIRED", output)
        self.assertEqual(len(calls), 1)

class PublishHTTPResourceTests(unittest.TestCase):
    """Live-shaped urllib errors are finalized inside capture, including delayed GC."""

    marker = "HTTP_RESOURCE_SECRET_CANARY"

    def exercise(self, status, *, package=False, close_failure=False):
        stderr, stdout = io.StringIO(), io.StringIO()
        response_bodies = []
        calls = []
        with patch("sys.stderr", stderr), patch("sys.stdout", stdout):
            with warnings.catch_warnings():
                warnings.simplefilter("always", ResourceWarning)
                with tempfile.TemporaryDirectory() as temporary:
                    values = {
                        **context(), "SOURCE_SHA": SHA, "RUNNER_TEMP": temporary,
                        "GITHUB_TOKEN": self.marker, "GITHUB_OUTPUT": str(Path(temporary) / "output"),
                        "PILOT_FRONTEND_REPOSITORY": "ghcr.io/next-stop-siren/frontend",
                        "PILOT_BACKEND_REPOSITORY": "ghcr.io/next-stop-siren/backend",
                    }
                    fixture = GitHubFake()
                    class Body(io.BytesIO):
                        def read(self, *args):
                            raise AssertionError("HTTP error body must not be read")

                        def close(self):
                            super().close()
                            if close_failure:
                                raise OSError(PublishHTTPResourceTests.marker)

                    def open_request(request, **kwargs):
                        path = urlsplit(request.full_url).path.lstrip("/")
                        is_package = "/packages/container/" in path
                        inject = is_package and fixture.package_reads < 2 if package else not calls
                        if inject:
                            calls.append("http_error")
                            fixture.package_reads += is_package
                            body = Body(self.marker.encode())
                            response_bodies.append(body)
                            raise urllib.error.HTTPError(
                                self.marker, status, self.marker, {"secret": self.marker}, body
                            )
                        data = fixture.get(path + ("?" + urlsplit(request.full_url).query
                                                   if urlsplit(request.full_url).query else ""))
                        return io.BytesIO(json.dumps(data).encode())

                    def run(arguments, **kwargs):
                        calls.append(arguments)
                        if "--metadata-file" in arguments:
                            Path(arguments[arguments.index("--metadata-file") + 1]).write_text(
                                json.dumps({"containerimage.digest": "sha256:" + "d" * 64})
                            )
                        return subprocess.CompletedProcess(arguments, 0, SHA if arguments[0] == "git" else "", "")

                    with (
                        patch.dict("os.environ", values, clear=True),
                        patch.object(sys, "argv", ["cli.py", "publish", "--execute"]),
                        patch("urllib.request.OpenerDirector.open", side_effect=open_request),
                        patch("subprocess.run", side_effect=run),
                    ):
                        # GitHub itself is real: request construction and error propagation are exercised.
                        self.assertIs(cli.GitHub, GitHub)
                        result = cli.main()
                    gc.collect()
                    exists = (Path(temporary) / "pilot-release" / "release.json").exists()
                    self.assertTrue(all(body.closed for body in response_bodies))
                gc.collect()
            gc.collect()
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(self.marker, output)
        self.assertNotIn("ResourceWarning", output)
        return result, output, exists, calls

    def test_real_github_403_response_closed_before_gc(self):
        result, output, exists, calls = self.exercise(403)
        self.assertEqual(result, 1)
        self.assertIn("stage=provenance; reason=GITHUB_ACCESS_OR_INPUT", output)
        self.assertFalse(exists)
        self.assertEqual(calls, ["http_error"])

    def test_first_creation_404_closes_both_responses_and_preserves_success(self):
        result, output, exists, calls = self.exercise(404, package=True)
        self.assertEqual(result, 0)
        self.assertEqual(output, "")
        self.assertTrue(exists)
        self.assertEqual(calls.count("http_error"), 2)
        self.assertEqual(sum("--push" in call for call in calls if isinstance(call, list)), 2)

    def test_package_403_is_closed_and_not_treated_as_first_creation(self):
        result, output, exists, calls = self.exercise(403, package=True)
        self.assertEqual(result, 1)
        self.assertIn("stage=package_preflight; reason=PACKAGE_LOOKUP_FAILED", output)
        self.assertFalse(exists)
        self.assertEqual(len(calls), 2)  # checkout + one denied package request, no Docker.

    def test_response_cleanup_failure_is_static_and_stops(self):
        for package in (False, True):
            with self.subTest(package=package):
                result, output, exists, _ = self.exercise(404 if package else 403,
                                                         package=package, close_failure=True)
                self.assertEqual(result, 1)
                self.assertIn("reason=HTTP_RESPONSE_CLEANUP_FAILED", output)
                self.assertFalse(exists)
