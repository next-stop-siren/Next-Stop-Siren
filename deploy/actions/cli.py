"""Thin workflow entry points; every mutation is explicit and gated."""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error

from contract import FILES, PUBLISH_WORKFLOW, REPOSITORY, canonical, digest, envelope, image_repository, matches
from contract import read_json, require, validate_directory, validate_release
from github_release import GitHub, authorized_context, download_release, environment_guard, publication_guard
from ssm_transport import AwsCLI, DispatchUnknown, configuration, dispatch

ROOT = Path(__file__).resolve().parents[2]


SAFE_VALIDATION_REASONS = {
    "aws_account_required",
    "aws_role",
    "explicit_seoul_region_required",
    "instance",
    "controlled_document_required",
    "numeric_pinned_document_version",
    "required_hashes",
    "image_repository",
    "source_sha",
    "publication_run_id",
    "same_sha_trusted_ci_success_required",
    "environment_policy",
    "environment_main_only",
    "environment_policy_count",
    "exact_main_sha",
    "owner_rerun",
    "manual_main_only",
    "pinned_document_description",
    "pinned_document_content",
    "remote_failed",
    "remote_nonzero",
    "command_failed",
    "package_lookup_failed",
}


def command(arguments, *, env=None, data=None):
    result = subprocess.run(arguments, input=data, capture_output=True, text=True, cwd=ROOT, env=env, timeout=900)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, arguments, output=result.stdout, stderr=result.stderr)
    return result.stdout.strip()


def close_publish_http_error(error):
    """Release error responses without reading their potentially sensitive bodies."""
    try:
        error.close()
    except Exception:
        raise ValueError("http_response_cleanup_failed") from None


class PublishFailure(ValueError):
    """Only static stage/reason identifiers cross the CLI diagnostic boundary."""

    def __init__(self, stage, error):
        self.stage = stage
        self.exit_code = None
        if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
            reason = "timeout_outcome_unknown"
        elif isinstance(error, subprocess.CalledProcessError):
            # Treat these bounded signals as hints, never echo command output or argv.
            output = error.stderr[-8192:].lower() if isinstance(error.stderr, str) else ""
            self.exit_code = error.returncode if type(error.returncode) is int else None
            registry_stage = stage == "registry_login" or stage in {"frontend_build_push", "backend_build_push"}
            if registry_stage and "unauthorized: authentication required" in output:
                reason = "authentication_rejected"
            elif registry_stage and (
                "denied: permission_denied" in output or "requested access to the resource is denied" in output
            ):
                reason = "registry_access_denied"
            elif stage in {"frontend_build_push", "backend_build_push"} and any(
                hint in output for hint in (
                    "multi-platform build is not supported for the docker driver",
                    "attestations are not supported for the docker driver",
                    "attestation is not supported for the docker driver",
                    "provenance is not supported",
                )
            ):
                reason = "builder_feature_unsupported"
            elif (
                stage in {"frontend_build_push", "backend_build_push"}
                and "failed to solve: process" in output
                and "did not complete successfully: exit code:" in output
            ):
                reason = "dockerfile_step_failed"
            else:
                reason = "command_failed_unknown_cause"
        elif isinstance(error, urllib.error.HTTPError):
            reason = "github_access_or_input" if error.code in {401, 403, 404} else "github_api_failed"
            try:
                close_publish_http_error(error)
            except ValueError:
                reason = "http_response_cleanup_failed"
        elif isinstance(error, urllib.error.URLError):
            reason = "github_transport_failed"
        elif isinstance(error, OSError):
            reason = (
                "command_launch_failed" if stage in {
                    "checkout", "registry_login", "frontend_build_push", "backend_build_push",
                    "builder_create", "builder_bootstrap", "builder_cleanup"
                } else "local_io_failed"
            )
        elif isinstance(error, ValueError):
            value = str(error)
            safe = SAFE_VALIDATION_REASONS | {"owner_public_package_required", "http_response_cleanup_failed"}
            reason = value if value in safe else "provenance_or_contract_rejected"
        else:
            reason = "unknown_failure"
        self.reason = reason
        super().__init__(reason)


@contextmanager
def publication_builder(execute, env):
    """Own one named builder in the publication's private temporary Docker config."""
    builder = "pilot-" + Path(env["DOCKER_CONFIG"]).name
    created, failed = False, False
    stage = "builder_create"
    try:
        execute(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container"], env=env)
        created = True
        stage = "builder_bootstrap"
        execute(["docker", "buildx", "inspect", builder, "--bootstrap"], env=env)
        stage = "builder_use"
        yield builder
    except BaseException as error:
        failed = True
        if isinstance(error, Exception) and not isinstance(error, PublishFailure):
            raise PublishFailure(stage, error) from None
        raise
    finally:
        if created:
            try:
                execute(["docker", "buildx", "rm", builder], env=env)
            except Exception as error:
                if not failed:
                    raise PublishFailure("builder_cleanup", error) from None
                # The original build/bootstrap failure remains authoritative.
                print("PILOT_BUILDER_CLEANUP_FAILED: reconcile owned builder before retry.", file=sys.stderr)


def build_image(execute, env, builder, name, image, sha, run_id, attempt):
    """Build one image using the explicit publication builder and validate its digest."""
    stage = name + "_build_push"
    try:
        metadata = Path(env["DOCKER_CONFIG"]) / (name + ".json")
        execute(
            [
                "docker", "buildx", "build", "--builder", builder,
                "--platform", "linux/amd64", "--provenance=true",
                "--label", "org.opencontainers.image.revision=" + sha,
                "--label", "org.opencontainers.image.source=https://github.com/" + REPOSITORY,
                "--file", f"deploy/{name}.Dockerfile",
                "--tag", f"{image}:sha-{sha}-run-{run_id}-{attempt}",
                "--metadata-file", str(metadata), "--push", ".",
            ],
            env=env,
        )
        stage = name + "_metadata"
        image_digest = read_json(metadata.read_bytes())["containerimage.digest"]
        require(matches(r"sha256:[0-9a-f]{64}", image_digest), "built_digest")
        return image_digest
    except Exception as error:
        raise PublishFailure(stage, error) from None


def publish(client, values, artifact_dir: Path, execute=command) -> str:
    stage = "provenance"
    try:
        context = authorized_context(values)
        sha = values.get("SOURCE_SHA", "")
        repo, workflow_id = publication_guard(client, sha, context)
        stage = "checkout"
        require(execute(["git", "rev-parse", "HEAD"]) == sha, "checkout_sha")
        stage = "package_preflight"
        images = [
            image_repository(values.get(name, "")) for name in ("PILOT_FRONTEND_REPOSITORY", "PILOT_BACKEND_REPOSITORY")
        ]
        require(images[0] != images[1], "distinct_packages")
        for image in images:
            try:
                package = client.get("orgs/next-stop-siren/packages/container/" + image.rsplit("/", 1)[1])
            except urllib.error.HTTPError as error:
                try:
                    require(error.code == 404, "package_lookup_failed")  # Confirmed first creation only.
                finally:
                    close_publish_http_error(error)
            else:
                require(package["visibility"] == "public", "owner_public_package_required")
        stage = "artifact_directory"
        artifact_dir.mkdir(parents=True, exist_ok=False)
        run_id, attempt = context["GITHUB_RUN_ID"], context["GITHUB_RUN_ATTEMPT"]
        stage = "docker_configuration"
        with tempfile.TemporaryDirectory(prefix="pilot-docker-", dir=values["RUNNER_TEMP"]) as docker_config:
            env = {**values, "DOCKER_CONFIG": docker_config}
            stage = "registry_login"
            execute(
                ["docker", "login", "ghcr.io", "--username", values["GITHUB_ACTOR"], "--password-stdin"],
                env=env,
                data=values["GITHUB_TOKEN"],
            )
            refs = []
            with publication_builder(execute, env) as builder:
                for name, image in zip(("frontend", "backend"), images, strict=True):
                    image_digest = build_image(execute, env, builder, name, image, sha, run_id, attempt)
                    refs.append(image + "@" + image_digest)
            stage = "docker_configuration_cleanup"
        stage = "public_package_verification"
        # First push can create private packages. Never release a deployable manifest until public.
        for image in images:
            package = client.get("orgs/next-stop-siren/packages/container/" + image.rsplit("/", 1)[1])
            require(package["visibility"] == "public", "owner_public_package_required")
        stage = "release_artifact"
        release = {
            "schema_version": 1,
            "repository": REPOSITORY,
            "repository_id": str(repo["id"]),
            "owner_id": str(repo["owner"]["id"]),
            "source_sha": sha,
            "workflow_id": str(workflow_id),
            "workflow_path": PUBLISH_WORKFLOW,
            "run_id": run_id,
            "run_attempt": attempt,
            "platform": "linux/amd64",
            "frontend": refs[0],
            "backend": refs[1],
            "files": {},
        }
        for name, source in FILES.items():
            data = (ROOT / source).read_bytes()
            (artifact_dir / name).write_bytes(data)
            release["files"][name] = digest(data)
        validate_release(release)
        (artifact_dir / "release.json").write_bytes(canonical(release))
        return f"pilot-release-{sha}-{run_id}-{attempt}"
    except PublishFailure:
        raise
    except Exception as error:
        raise PublishFailure(stage, error) from None


def prepare(client, values, directory: Path, execute=command) -> None:
    context = authorized_context(values)
    require(execute(["git", "rev-parse", "HEAD"]) == context["GITHUB_SHA"], "checkout_sha")
    environment_guard(client, "pilot")
    config = configuration(values)  # Required Owner inputs before requesting OIDC credentials.
    release = download_release(client, values.get("PUBLICATION_RUN_ID", ""), directory)
    (directory.parent / "deployment-envelope.txt").write_text(envelope(release, config["gates_hash"]))
    (directory.parent / "deployment-config.json").write_bytes(canonical(config))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("publish", "prepare", "dispatch"))
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args()
    publish_stage = "context"
    try:
        authorized_context()
        temporary = Path(os.environ["RUNNER_TEMP"]) / "pilot-release"
        if args.operation == "publish":
            publish_stage = "publication_setup"
            name = publish(GitHub(os.environ["GITHUB_TOKEN"]), dict(os.environ), temporary)
            publish_stage = "github_output"
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write("artifact_name=" + name + "\n")
        elif args.operation == "prepare":
            prepare(GitHub(os.environ["GITHUB_TOKEN"]), dict(os.environ), temporary)
        else:
            config = configuration()
            require(read_json((temporary.parent / "deployment-config.json").read_bytes()) == config, "config_changed")
            encoded = (temporary.parent / "deployment-envelope.txt").read_text()
            require(encoded == envelope(validate_directory(temporary), config["gates_hash"]), "prepared_bundle_changed")
            document = read_json((temporary / "ssm-document.json").read_bytes())
            command_id = dispatch(AwsCLI(config["region"]), config, encoded, document)
            print("Verified SSM success; command ID " + command_id)
    except Exception as error:
        if args.operation == "publish":
            failure = error if isinstance(error, PublishFailure) else PublishFailure(publish_stage, error)
            if failure.reason == "owner_public_package_required":
                print(
                    "Owner action: make both GHCR packages public in GitHub package settings; "
                    "verify package state before rerunning publication; no release issued.", file=sys.stderr,
                )
            print(
                "PILOT_PUBLISH_FAILED: stage=" + failure.stage + "; reason=" + failure.reason.upper()
                + ("; exit=" + str(failure.exit_code) if failure.exit_code is not None else "")
                + "; stop; no blind resend; see deployment documentation.",
                file=sys.stderr,
            )
            return 1
        if isinstance(error, ValueError) and str(error) == "owner_public_package_required":
            print(
                "Owner action: make both GHCR packages public in GitHub package settings, "
                "then rerun publication; no release issued.",
                file=sys.stderr,
            )
        else:
            if isinstance(error, DispatchUnknown):
                code = "DISPATCH_UNKNOWN_DO_NOT_RESEND"
            elif isinstance(error, subprocess.CalledProcessError):
                code = "COMMAND_FAILED"
            elif isinstance(error, TimeoutError):
                code = "TIMEOUT_RECONCILE_COMMAND"
            elif isinstance(error, urllib.error.HTTPError):
                code = "GITHUB_API_ACCESS_OR_INPUT"
            elif isinstance(error, ValueError):
                # Only locally generated constant validation reasons, never arbitrary API/env values.
                reason = str(error)
                safe = SAFE_VALIDATION_REASONS
                code = reason.upper() if reason in safe else "PROVENANCE_OR_CONTRACT_REJECTED"
            else:
                code = "LOCAL_OR_REMOTE_OPERATION_FAILED"
            print(
                "PILOT_ACTION_FAILED: " + code + "; stop; no blind resend; see deployment documentation.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
