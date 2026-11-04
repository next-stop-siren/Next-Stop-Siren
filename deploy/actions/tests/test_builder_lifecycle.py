"""Offline acceptance of owned buildx lifecycle; every external operation is fake."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
from test_actions import context, GitHubFake, SHA


class BuilderLifecycleTests(unittest.TestCase):
    def exercise(self, failing=None, cleanup_fails=False):
        calls, output = [], io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            values = {
                **context(), "SOURCE_SHA": SHA, "RUNNER_TEMP": temporary,
                "GITHUB_TOKEN": "BUILDER_SECRET_CANARY",
                "PILOT_FRONTEND_REPOSITORY": "ghcr.io/next-stop-siren/frontend",
                "PILOT_BACKEND_REPOSITORY": "ghcr.io/next-stop-siren/backend",
            }
            def execute(args, **kwargs):
                operation = args[2] if args[0] == "docker" and args[1] == "buildx" else args[0]
                if operation == "build":
                    operation = Path(args[args.index("--file") + 1]).stem
                calls.append((operation, list(args), kwargs.get("env")))
                if operation == failing or operation == "rm" and cleanup_fails:
                    raise subprocess.CalledProcessError(7, ["BUILDER_SECRET_CANARY"], stderr="BUILDER_SECRET_CANARY")
                if "--metadata-file" in args:
                    Path(args[args.index("--metadata-file") + 1]).write_text(
                        json.dumps({"containerimage.digest": "sha256:" + "d" * 64})
                    )
                if kwargs.get("env"):
                    self.assertTrue(Path(kwargs["env"]["DOCKER_CONFIG"]).is_dir())
                return SHA if args[0] == "git" else ""
            error = None
            with (
                contextlib.redirect_stderr(output),
                patch("subprocess.run", side_effect=AssertionError("process forbidden")),
            ):
                with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
                    try:
                        cli.publish(GitHubFake(), values, Path(temporary) / "release", execute)
                    except cli.PublishFailure as failure:
                        error = failure
            release_exists = (Path(temporary) / "release" / "release.json").exists()
            configs = [env["DOCKER_CONFIG"] for _, _, env in calls if env]
            self.assertTrue(all(not Path(config).exists() for config in configs))
        self.assertNotIn("BUILDER_SECRET_CANARY", output.getvalue())
        return calls, error, release_exists, output.getvalue()

    def test_pair_uses_same_owned_container_builder_and_private_config(self):
        calls, error, release, _ = self.exercise()
        self.assertIsNone(error)
        self.assertTrue(release)
        self.assertEqual(
            [op for op, _, _ in calls], ["git", "docker", "create", "inspect", "frontend", "backend", "rm"]
        )
        docker = [(args, env) for _, args, env in calls if env]
        config = docker[0][1]["DOCKER_CONFIG"]
        self.assertTrue(all(env["DOCKER_CONFIG"] == config for _, env in docker))
        create = calls[2][1]
        builder = create[create.index("--name") + 1]
        self.assertTrue(builder.startswith("pilot-pilot-docker-"))
        self.assertEqual(create[create.index("--driver") + 1], "docker-container")
        self.assertEqual(calls[3][1], ["docker", "buildx", "inspect", builder, "--bootstrap"])
        self.assertEqual(calls[-1][1], ["docker", "buildx", "rm", builder])
        for _, args, _ in calls[4:6]:
            self.assertEqual(args[args.index("--builder") + 1], builder)
            self.assertIn("--provenance=true", args)
            self.assertIn("--push", args)

    def test_failed_create_never_removes_unconfirmed_builder(self):
        calls, error, release, _ = self.exercise("create")
        self.assertEqual(error.stage, "builder_create")
        self.assertNotIn("rm", [op for op, _, _ in calls])
        self.assertFalse(release)

    def test_bootstrap_and_each_image_failure_remove_only_owned_builder(self):
        for failing, stage in (("inspect", "builder_bootstrap"), ("frontend", "frontend_build_push"),
                               ("backend", "backend_build_push")):
            with self.subTest(failing=failing):
                calls, error, release, _ = self.exercise(failing)
                self.assertEqual(error.stage, stage)
                self.assertEqual(error.exit_code, 7)
                self.assertFalse(release)
                self.assertEqual([op for op, _, _ in calls].count("rm"), 1)
                if failing != "backend":
                    self.assertNotIn("backend", [op for op, _, _ in calls])

    def test_cleanup_failure_prevents_release_and_preserves_primary_failure(self):
        _, error, release, _ = self.exercise(cleanup_fails=True)
        self.assertEqual(error.stage, "builder_cleanup")
        self.assertFalse(release)
        for failing, stage in (("inspect", "builder_bootstrap"), ("frontend", "frontend_build_push")):
            _, error, release, output = self.exercise(failing, cleanup_fails=True)
            self.assertEqual(error.stage, stage)
            self.assertFalse(release)
            self.assertIn("PILOT_BUILDER_CLEANUP_FAILED", output)
