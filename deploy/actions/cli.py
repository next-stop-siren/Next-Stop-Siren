"""Thin workflow entry points; every mutation is explicit and gated."""

import argparse
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


def command(arguments, *, env=None, data=None):
    result = subprocess.run(arguments, input=data, capture_output=True, text=True, cwd=ROOT, env=env, timeout=900)
    require(result.returncode == 0, "command_failed")
    return result.stdout.strip()


def publish(client, values, artifact_dir: Path, execute=command) -> str:
    context = authorized_context(values)
    sha = values.get("SOURCE_SHA", "")
    repo, workflow_id = publication_guard(client, sha, context)
    require(execute(["git", "rev-parse", "HEAD"]) == sha, "checkout_sha")
    images = [
        image_repository(values.get(name, "")) for name in ("PILOT_FRONTEND_REPOSITORY", "PILOT_BACKEND_REPOSITORY")
    ]
    require(images[0] != images[1], "distinct_packages")
    for image in images:
        try:
            package = client.get("orgs/next-stop-siren/packages/container/" + image.rsplit("/", 1)[1])
        except urllib.error.HTTPError as error:
            require(error.code == 404, "package_lookup_failed")  # Confirmed first creation only.
        else:
            require(package["visibility"] == "public", "owner_public_package_required")
    artifact_dir.mkdir(parents=True, exist_ok=False)
    run_id, attempt = context["GITHUB_RUN_ID"], context["GITHUB_RUN_ATTEMPT"]
    with tempfile.TemporaryDirectory(prefix="pilot-docker-", dir=values["RUNNER_TEMP"]) as docker_config:
        env = {**values, "DOCKER_CONFIG": docker_config}
        execute(
            ["docker", "login", "ghcr.io", "--username", values["GITHUB_ACTOR"], "--password-stdin"],
            env=env,
            data=values["GITHUB_TOKEN"],
        )
        refs = []
        for name, image in zip(("frontend", "backend"), images, strict=True):
            metadata = Path(docker_config) / (name + ".json")
            execute(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--provenance=true",
                    "--label",
                    "org.opencontainers.image.revision=" + sha,
                    "--label",
                    "org.opencontainers.image.source=https://github.com/" + REPOSITORY,
                    "--file",
                    f"deploy/{name}.Dockerfile",
                    "--tag",
                    f"{image}:sha-{sha}-run-{run_id}-{attempt}",
                    "--metadata-file",
                    str(metadata),
                    "--push",
                    ".",
                ],
                env=env,
            )
            image_digest = read_json(metadata.read_bytes())["containerimage.digest"]
            require(matches(r"sha256:[0-9a-f]{64}", image_digest), "built_digest")
            refs.append(image + "@" + image_digest)
    # First push can create private packages. Never release a deployable manifest until public.
    for image in images:
        package = client.get("orgs/next-stop-siren/packages/container/" + image.rsplit("/", 1)[1])
        require(package["visibility"] == "public", "owner_public_package_required")
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
    try:
        authorized_context()
        temporary = Path(os.environ["RUNNER_TEMP"]) / "pilot-release"
        if args.operation == "publish":
            name = publish(GitHub(os.environ["GITHUB_TOKEN"]), dict(os.environ), temporary)
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
        if isinstance(error, ValueError) and str(error) == "owner_public_package_required":
            print(
                "Owner action: make both GHCR packages public in GitHub package settings, "
                "then rerun publication; no release issued.",
                file=sys.stderr,
            )
        else:
            if isinstance(error, DispatchUnknown):
                code = "DISPATCH_UNKNOWN_DO_NOT_RESEND"
            elif isinstance(error, TimeoutError):
                code = "TIMEOUT_RECONCILE_COMMAND"
            elif isinstance(error, urllib.error.HTTPError):
                code = "GITHUB_API_ACCESS_OR_INPUT"
            elif isinstance(error, ValueError):
                # Only locally generated constant validation reasons, never arbitrary API/env values.
                reason = str(error)
                safe = {
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
