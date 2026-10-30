"""Offline acceptance probes. All GitHub/AWS/Docker effects use fakes."""

import copy
import fcntl
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
from contract import FILES, CI_WORKFLOW, PUBLISH_WORKFLOW, REPOSITORY, canonical, digest, envelope, validate_directory
from github_release import authorized_context, download_release, publication_guard
from ssm_transport import DispatchUnknown, InvocationPending, configuration, dispatch
import server_deploy as host

ROOT = Path(__file__).resolve().parents[3]
SHA = "a" * 40
COMMAND_ID = "12345678-1234-1234-1234-123456789abc"


def setUpModule():
    # Also active for unittest discovery, with nested mocks opting into explicit fake subprocesses.
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


def release():
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "repository_id": "101",
        "owner_id": "102",
        "source_sha": SHA,
        "workflow_id": "92",
        "workflow_path": PUBLISH_WORKFLOW,
        "run_id": "111",
        "run_attempt": "1",
        "platform": "linux/amd64",
        "frontend": "ghcr.io/next-stop-siren/frontend@sha256:" + "b" * 64,
        "backend": "ghcr.io/next-stop-siren/backend@sha256:" + "c" * 64,
        "files": {name: digest((ROOT / source).read_bytes()) for name, source in FILES.items()},
    }


def context():
    return {
        "GITHUB_SHA": SHA,
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "xifoxy-ru",
        "GITHUB_TRIGGERING_ACTOR": "xifoxy-ru",
        "GITHUB_RUN_ID": "111",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY_ID": "101",
        "GITHUB_REPOSITORY_OWNER_ID": "102",
    }


def archive(value=None, extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, source in FILES.items():
            bundle.writestr(name, (ROOT / source).read_bytes())
        bundle.writestr("release.json", canonical(value or release()))
        if extra:
            bundle.writestr(*extra)
    return output.getvalue()


class GitHubFake:
    """REST-shaped fixtures: workflow runs, artifacts and environment branch policies."""

    def __init__(self):
        self.requests = []
        self.repo = {"id": 101, "owner": {"id": 102}}
        common = {
            "repository": {"id": 101},
            "head_repository": {"id": 101, "full_name": REPOSITORY},
            "head_branch": "main",
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
        }
        self.ci = {**copy.deepcopy(common), "workflow_id": 91, "path": CI_WORKFLOW, "event": "push"}
        self.run = {
            **copy.deepcopy(common),
            "workflow_id": 92,
            "path": PUBLISH_WORKFLOW,
            "event": "workflow_dispatch",
            "run_attempt": 1,
            "actor": {"login": "xifoxy-ru"},
            "triggering_actor": {"login": "xifoxy-ru"},
        }
        self.zip = archive()
        self.visibility = "public"
        self.first_creation = False
        self.package_reads = 0
        self.artifact_digest = None
        self.policy = {"name": "main"}  # Real REST example omits type.

    def get(self, path):
        self.requests.append(path)
        if path == f"repos/{REPOSITORY}":
            return self.repo
        if "/deployment-branch-policies" in path:
            return {"total_count": 1, "branch_policies": [self.policy]}
        if "/environments/" in path:
            return {"deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False}}
        if "/git/ref/" in path:
            return {"object": {"sha": SHA}}
        if "/workflows/91/runs?" in path:
            return {"workflow_runs": [self.ci]}
        if path.endswith("workflows/ci.yml"):
            return {"id": 91}
        if path.endswith("workflows/publish-pilot.yml"):
            return {"id": 92}
        if path.endswith("/runs/111"):
            return self.run
        if "/compare/" in path:
            return {"status": getattr(self, "compare_status", "identical")}
        if path.endswith("/runs/111/artifacts"):
            return {
                "artifacts": [
                    {
                        "id": 123,
                        "name": f"pilot-release-{SHA}-111-1",
                        "expired": False,
                        "workflow_run": {"id": 111, "head_sha": SHA},
                        "digest": self.artifact_digest or "sha256:" + digest(self.zip),
                    }
                ]
            }
        if "/packages/container/" in path:
            self.package_reads += 1
            if self.first_creation and self.package_reads <= 2:
                raise urllib.error.HTTPError("redacted", 404, "missing", {}, None)
            return {"visibility": self.visibility}
        raise AssertionError("Unexpected offline GitHub request " + path)

    def artifact_zip(self, artifact_id):
        assert artifact_id == 123
        return self.zip


def validate_pin_proof(proof, pin):
    """Preparation-only evidence checker, not a runtime remote resolver."""
    repository = proof["repository"]
    if repository not in {"actions/checkout", "actions/upload-artifact", "aws-actions/configure-aws-credentials"}:
        raise ValueError("untrusted_action_repository")
    prefix = "repos/" + repository + "/git/"

    def successful(record, endpoint):
        if record["command"] != ["gh", "api", prefix + endpoint] or record["exit"] != 0:
            raise ValueError("unsuccessful_official_response")
        return record["body"]

    current = proof.get("ref")
    if current:
        body = successful(current, "ref/tags/" + proof["tag"])
        target = body["object"]
        for record in proof["tags"]:
            if target["type"] != "tag":
                raise ValueError("unexpected_tag")
            body = successful(record, "tags/" + target["sha"])
            if body["sha"] != target["sha"]:
                raise ValueError("tag_identity")
            target = body["object"]
        if target != {"type": "commit", "sha": pin, "url": "https://api.github.com/" + prefix + "commits/" + pin}:
            raise ValueError("not_final_commit")
    elif proof["tags"]:
        raise ValueError("orphan_tag")
    body = successful(proof["commit"], "commits/" + pin)
    if (
        body.get("sha") != pin
        or body.get("url") != "https://api.github.com/" + prefix + "commits/" + pin
        or not isinstance(body.get("tree"), dict)
        or not isinstance(body.get("parents"), list)
    ):
        raise ValueError("commit_object_required")
    return pin


class PreparationTests(unittest.TestCase):
    def test_real_prepare_rejects_checkout_mismatch_before_artifact(self):
        github = GitHubFake()
        with tempfile.TemporaryDirectory() as temporary, patch("cli.configuration") as config_mock:
            with self.assertRaisesRegex(ValueError, "checkout_sha"):
                cli.prepare(
                    github,
                    {**context(), "PUBLICATION_RUN_ID": "111"},
                    Path(temporary) / "release",
                    execute=lambda arguments: "f" * 40,
                )
            self.assertEqual(github.requests, [])
            config_mock.assert_not_called()
            self.assertFalse((Path(temporary) / "release").exists())

    def test_real_prepare_matching_workflow_allows_older_release(self):
        github = GitHubFake()
        github.compare_status = "ahead"  # Publication SHA precedes trusted current main.
        values = {**context(), "GITHUB_SHA": "f" * 40, "PUBLICATION_RUN_ID": "111"}
        with tempfile.TemporaryDirectory() as temporary, patch("cli.configuration", return_value=config()):
            directory = Path(temporary) / "release"
            cli.prepare(github, values, directory, execute=lambda arguments: values["GITHUB_SHA"])
            self.assertEqual(validate_directory(directory)["source_sha"], SHA)
            self.assertNotEqual(SHA, values["GITHUB_SHA"])
            self.assertTrue((directory.parent / "deployment-envelope.txt").exists())

    def test_ref_proof_requires_commit_not_tag_or_404(self):
        repository = "aws-actions/configure-aws-credentials"
        pin, tag = "a" * 40, "b" * 40
        prefix = "repos/" + repository + "/git/"
        api = "https://api.github.com/" + prefix

        def record(endpoint, body):
            return {"command": ["gh", "api", prefix + endpoint], "exit": 0, "body": body}

        target = {"type": "commit", "sha": pin, "url": api + "commits/" + pin}
        proof = {
            "repository": repository,
            "tag": "v5",
            "ref": record("ref/tags/v5", {"object": {"type": "tag", "sha": tag, "url": api + "tags/" + tag}}),
            "tags": [record("tags/" + tag, {"sha": tag, "object": target})],
            "commit": record("commits/" + pin, {"sha": pin, "url": api + "commits/" + pin, "tree": {}, "parents": []}),
        }
        self.assertEqual(validate_pin_proof(proof, pin), pin)
        for failure in ("tag", "404", "wrong_endpoint", "wrong_commit"):
            with self.subTest(failure=failure):
                bad = copy.deepcopy(proof)
                if failure == "tag":
                    bad["tags"] = []
                elif failure == "404":
                    bad["commit"]["exit"] = 1
                    bad["commit"]["body"] = {"message": "Not Found", "status": "404"}
                elif failure == "wrong_endpoint":
                    bad["commit"]["command"][-1] = "repos/attacker/repo/git/commits/" + pin
                else:
                    bad["commit"]["body"]["sha"] = tag
                with self.assertRaises(ValueError):
                    validate_pin_proof(bad, pin)


class WorkflowTests(unittest.TestCase):
    def test_manual_only_scoped_permissions_owner_and_pinned_refs(self):
        for name in ("publish-pilot.yml", "deploy-pilot.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            self.assertIn("  workflow_dispatch:", text)
            self.assertNotIn("  push:", text)
            self.assertNotIn("  pull_request:", text)
            self.assertIn("github.triggering_actor == 'xifoxy-ru'", text)
            self.assertIn("github.actor == 'xifoxy-ru'", text)
            self.assertIn("github.ref == 'refs/heads/main'", text)
            self.assertIn("cancel-in-progress: false", text)
            self.assertIn("persist-credentials: false", text)
            self.assertIn("ref: ${{ github.sha }}", text)
            self.assertNotIn("run: ${{", text)
            for line in text.splitlines():
                if "uses:" in line:
                    pin = line.split("@", 1)[1].split()[0]
                    self.assertRegex(pin, r"^[0-9a-f]{40}$")
            if name.startswith("publish"):
                self.assertIn("packages: write", text)
                self.assertNotIn("id-token:", text)
                self.assertIn("overwrite: false", text)
            else:
                self.assertNotIn("packages: write", text)
                self.assertIn("id-token: write", text)
                self.assertLess(text.index("cli.py prepare"), text.index("aws-actions/configure"))
                self.assertLess(text.index("aws-actions/configure"), text.index("cli.py dispatch"))


class ProvenanceTests(unittest.TestCase):
    def test_exact_ci_identity_and_owner_rerun(self):
        github = GitHubFake()
        publication_guard(github, SHA, authorized_context(context()))
        for field, wrong in (
            ("workflow_id", 999),
            ("event", "pull_request"),
            ("path", "other.yml"),
            ("head_sha", "f" * 40),
            ("head_branch", "evil"),
        ):
            with self.subTest(field=field):
                bad = GitHubFake()
                bad.ci[field] = wrong
                with self.assertRaises(ValueError):
                    publication_guard(bad, SHA, authorized_context(context()))
        for field in ("GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR", "GITHUB_REF"):
            values = context()
            values[field] = "untrusted"
            with self.assertRaises(ValueError):
                authorized_context(values)
        with self.assertRaises(ValueError):
            publication_guard(github, SHA + ";echo injected", authorized_context(context()))

    def test_ci_fork_and_environment_tag_rejected(self):
        github = GitHubFake()
        github.ci["head_repository"]["id"] = 999
        with self.assertRaises(ValueError):
            publication_guard(github, SHA, authorized_context(context()))
        github = GitHubFake()
        github.policy["type"] = "tag"
        with self.assertRaises(ValueError):
            publication_guard(github, SHA, authorized_context(context()))

    def test_archive_valid_and_negative_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release"
            self.assertEqual(download_release(GitHubFake(), "111", path), validate_directory(path))
        for mutation in ("digest", "attempt", "actor", "traversal", "wrong_manifest", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                github = GitHubFake()
                if mutation == "digest":
                    github.artifact_digest = "sha256:" + "0" * 64
                elif mutation == "attempt":
                    github.run["run_attempt"] = 2
                elif mutation == "actor":
                    github.run["triggering_actor"]["login"] = "other"
                elif mutation == "traversal":
                    github.zip = archive(extra=("../escape", b"bad"))
                elif mutation == "wrong_manifest":
                    value = release()
                    value["source_sha"] = "f" * 40
                    github.zip = archive(value)
                else:
                    info = zipfile.ZipInfo("unexpected")
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    github.zip = archive(extra=(info, b"release.json"))
                with self.assertRaises(ValueError):
                    download_release(github, "111", Path(temporary) / "release")

    def test_first_publication_public_gate_and_same_commit_pair(self):
        for visibility in ("public", "private"):
            with self.subTest(visibility=visibility), tempfile.TemporaryDirectory() as temporary:
                values = {
                    **context(),
                    "SOURCE_SHA": SHA,
                    "RUNNER_TEMP": temporary,
                    "GITHUB_TOKEN": "test-only-token",
                    "PILOT_FRONTEND_REPOSITORY": "ghcr.io/next-stop-siren/frontend",
                    "PILOT_BACKEND_REPOSITORY": "ghcr.io/next-stop-siren/backend",
                }
                github = GitHubFake()
                github.first_creation, github.visibility = True, visibility
                calls = []

                def execute(arguments, **kwargs):
                    calls.append(arguments)
                    if arguments[0] == "git":
                        return SHA
                    if "--metadata-file" in arguments:
                        Path(arguments[arguments.index("--metadata-file") + 1]).write_text(
                            json.dumps({"containerimage.digest": "sha256:" + "d" * 64})
                        )
                    return ""

                path = Path(temporary) / "release"
                if visibility == "private":
                    with self.assertRaisesRegex(ValueError, "owner_public_package_required"):
                        cli.publish(github, values, path, execute)
                    self.assertFalse((path / "release.json").exists())
                else:
                    cli.publish(github, values, path, execute)
                    self.assertEqual(validate_directory(path)["source_sha"], SHA)
                builds = [call for call in calls if "--push" in call]
                self.assertEqual(len(builds), 2)
                for call in builds:
                    self.assertIn("linux/amd64", call)
                    self.assertIn("org.opencontainers.image.revision=" + SHA, call)


def config():
    return {
        "account": "123456789012",
        "role": "arn:aws:iam::123456789012:role/Pilot",
        "region": "ap-northeast-2",
        "instance": "i-1234567890abcdef0",
        "document": "PilotDeploy",
        "version": "1",
        "document_hash": "d" * 64,
        "gates_hash": "e" * 64,
    }


class SSMFake:
    def __init__(self, states=None, unknown=False):
        self.calls = []
        self.states = list(states or [{"Status": "Success", "ResponseCode": 0}])
        self.unknown = unknown
        self.description = {
            "Hash": "d" * 64,
            "HashType": "Sha256",
            "DocumentVersion": "1",
            "Name": "PilotDeploy",
            "Owner": "123456789012",
            "Status": "Active",
            "DocumentType": "Command",
        }
        self.document = json.loads((ROOT / "deploy/actions/ssm-document.json").read_text())

    def call(self, operation, arguments):
        self.calls.append(operation)
        if operation == "describe-document":
            return {"Document": self.description}
        if operation == "get-document":
            # API_GetDocument response has Content but NO Sha256/Hash.
            return {
                "Content": json.dumps(self.document),
                "DocumentVersion": "1",
                "Name": "PilotDeploy",
                "Status": "Active",
                "DocumentType": "Command",
            }
        if operation == "send-command":
            if self.unknown:
                raise TimeoutError("unknown")
            return {"Command": {"CommandId": COMMAND_ID}}
        if operation == "get-command-invocation":
            value = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            if isinstance(value, Exception):
                raise value
            return value
        raise AssertionError(operation)


class SSMTests(unittest.TestCase):
    def dispatch(self, fake, timeout=10):
        now = [0]
        return dispatch(
            fake,
            config(),
            envelope(release(), "e" * 64),
            fake.document,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            timeout_seconds=timeout,
        )

    def test_real_document_shape_eventual_consistency_and_success(self):
        fake = SSMFake(
            [
                InvocationPending(),
                {"Status": "Pending"},
                {"Status": "InProgress"},
                {"Status": "Success", "ResponseCode": 0},
            ]
        )
        self.assertEqual(self.dispatch(fake, 20), COMMAND_ID)
        self.assertEqual(fake.calls.count("send-command"), 1)

    def test_failure_nonzero_and_bounded_timeout(self):
        for result in (
            {"Status": "Failed", "ResponseCode": 1},
            {"Status": "TimedOut"},
            {"Status": "Success", "ResponseCode": 5},
            {"Status": "Pending"},
        ):
            with self.subTest(result=result):
                fake = SSMFake([result])
                with self.assertRaises((ValueError, TimeoutError)):
                    self.dispatch(fake)
                self.assertEqual(fake.calls.count("send-command"), 1)
                self.assertNotIn("cancel-command", fake.calls)

    def test_unknown_send_no_retry_and_pinned_document_mismatch(self):
        fake = SSMFake(unknown=True)
        with self.assertRaises(DispatchUnknown):
            self.dispatch(fake)
        self.assertEqual(fake.calls.count("send-command"), 1)
        fake = SSMFake()
        fake.description["Hash"] = "0" * 64
        with self.assertRaises(ValueError):
            self.dispatch(fake)
        self.assertNotIn("send-command", fake.calls)

    def test_missing_config_and_injection_fail_closed(self):
        with self.assertRaises(ValueError):
            configuration({})
        values = {
            "PILOT_AWS_ACCOUNT_ID": "123456789012",
            "PILOT_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/Pilot",
            "PILOT_AWS_REGION": "ap-northeast-2",
            "PILOT_INSTANCE_ID": "i-1234567890abcdef0",
            "PILOT_SSM_DOCUMENT_NAME": "PilotDeploy;rm",
            "PILOT_SSM_DOCUMENT_VERSION": "1",
            "PILOT_SSM_DOCUMENT_SHA256": "d" * 64,
            "PILOT_GATES_SHA256": "e" * 64,
        }
        with self.assertRaises(ValueError):
            configuration(values)
        with self.assertRaises(ValueError):
            dispatch(SSMFake(), config(), "$(bad)", {})

    def test_aws_cli_disables_send_retries_without_exposing_output(self):
        from ssm_transport import AwsCLI

        with patch("ssm_transport.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            AwsCLI("ap-northeast-2").call("send-command", [])
            self.assertEqual(run.call_args.kwargs["env"]["AWS_MAX_ATTEMPTS"], "1")
            self.assertEqual(run.call_args.kwargs["timeout"], 45)


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "host"
        self.runtime, self.gates, self.functional = [self.base / name for name in ("runtime", "gates", "functional")]
        self.runtime.write_text("TEST_ONLY=1\n")
        self.functional.write_text("test-only-not-executed")
        self.functional.chmod(0o700)
        gates = {
            "schema_version": 1,
            "source_sha": SHA,
            "functional_check_sha256": digest(self.functional.read_bytes()),
            "certbot_image": "certbot/certbot@sha256:" + "f" * 64,
        }
        for name in ("readiness", "trusted_proxy", "data_recovery"):
            gates[name] = {"status": "approved", "evidence_ref": "offline-fixture", "sha256": "1" * 64}
        self.gates.write_bytes(canonical(gates))
        self.data = {"release": release(), "gates_sha256": digest(self.gates.read_bytes())}
        self.calls = []

    def runner(self, arguments, lock_fd, deadline):
        self.calls.append(arguments)
        self.assertGreater(deadline, 0)
        with (self.root / "deploy.lock").open("a+") as second:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def deploy(self, **changes):
        options = dict(
            root=self.root,
            runtime=self.runtime,
            gates_path=self.gates,
            functional=self.functional,
            fetch=lambda sha: (ROOT / "deploy/compose.yaml").read_bytes(),
            runner=self.runner,
            check_trusted=lambda *args, **kwargs: None,
            identity_check=lambda *args: True,
        )
        options.update(changes)
        return host.deploy(self.data, **options)

    def test_order_nginx_refresh_lock_and_checked_noop(self):
        self.assertEqual(self.deploy(), "applied_checked")
        ups = [call for call in self.calls if "up" in call]
        self.assertEqual([call[-1] for call in ups], ["db", "backend", "nginx"])
        self.assertIn("--no-recreate", ups[0])
        self.assertIn("--force-recreate", ups[2])
        self.assertFalse((self.root / "inflight.json").exists())
        self.calls.clear()
        self.assertEqual(self.deploy(), "already_applied_checked")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], str(self.functional))
        states = iter((False, True))
        self.calls.clear()
        self.assertEqual(self.deploy(identity_check=lambda *args: next(states)), "applied_checked")
        self.assertTrue(any("up" in call for call in self.calls))

    def test_failed_rollout_keeps_inflight_and_blocks_retry(self):
        def fail(arguments, *args):
            self.runner(arguments, *args)
            if arguments[-1] == "backend" and "up" in arguments:
                raise RuntimeError("offline failure")

        with self.assertRaises(RuntimeError):
            self.deploy(runner=fail)
        self.assertTrue((self.root / "inflight.json").exists())
        with self.assertRaisesRegex(ValueError, "owner_reconciliation_required"):
            self.deploy()
        self.assertFalse(any("down" in call or "restore" in call or "build" in call for call in self.calls))

    def test_unapproved_gates_and_tampered_compose_fail_before_rollout(self):
        self.data["gates_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.deploy()
        self.assertEqual(self.calls, [])
        self.data["gates_sha256"] = digest(self.gates.read_bytes())
        with self.assertRaises(ValueError):
            self.deploy(fetch=lambda sha: b"tampered")
        self.assertEqual(self.calls, [])

    def test_container_digest_identity_mismatch(self):
        responses = ["a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64]
        with patch(
            "server_deploy.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0, v, "") for v in responses]
        ):
            self.assertFalse(host.verify_images(["docker", "compose"], release()))

    def test_child_inherits_lock_and_timeout_kills_group(self):
        process = unittest.mock.Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired("fake", 1), 0]
        with (
            patch("server_deploy.subprocess.Popen", return_value=process) as popen,
            patch("server_deploy.os.killpg") as kill,
        ):
            with self.assertRaises(TimeoutError):
                host.run_command(["fake-not-executed"], 17, host.time.monotonic() + 10)
            self.assertEqual(popen.call_args.kwargs["pass_fds"], (17,))
            self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
            kill.assert_called_once()


if __name__ == "__main__":
    # Accidental live calls fail deterministically across every test.
    with (
        patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")),
        patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")),
    ):
        unittest.main(verbosity=2)
