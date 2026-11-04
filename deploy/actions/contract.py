"""Closed public release contract shared by runner and pre-provisioned host."""

import base64
import hashlib
import json
import re
from pathlib import Path

REPOSITORY = "next-stop-siren/Next-Stop-Siren"
OWNER = "xifoxy-ru"
PUBLISH_WORKFLOW = ".github/workflows/publish-pilot.yml"
CI_WORKFLOW = ".github/workflows/ci.yml"
FILES = {
    "compose.yaml": "deploy/compose.yaml",
    "server_deploy.py": "deploy/actions/server_deploy.py",
    "contract.py": "deploy/actions/contract.py",
    "ssm-document.json": "deploy/actions/ssm-document.json",
}
HEX = r"[0-9a-f]{64}"
SHA = r"[0-9a-f]{40}"


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def matches(pattern: str, value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def read_json(data: bytes | str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("nonfinite_json")

    result = json.loads(data, object_pairs_hook=pairs, parse_constant=reject_constant)
    require(isinstance(result, dict), "json_object_required")
    return result


def image_repository(value: str) -> str:
    require(matches(r"ghcr\.io/next-stop-siren/[a-z0-9][a-z0-9._-]{0,100}", value), "image_repository")
    return value


def validate_release(release: dict) -> dict:
    keys = {
        "schema_version",
        "repository",
        "repository_id",
        "owner_id",
        "source_sha",
        "workflow_id",
        "workflow_path",
        "run_id",
        "run_attempt",
        "platform",
        "frontend",
        "backend",
        "files",
    }
    require(set(release) == keys and release["schema_version"] == 1, "release_shape")
    require(release["repository"] == REPOSITORY and release["workflow_path"] == PUBLISH_WORKFLOW, "release_origin")
    for field in ("repository_id", "owner_id", "workflow_id", "run_id", "run_attempt"):
        require(matches(r"[1-9][0-9]*", release[field]), "release_identity")
    require(matches(SHA, release["source_sha"]) and release["platform"] == "linux/amd64", "release_source")
    for name in ("frontend", "backend"):
        value = release[name]
        require(isinstance(value, str) and "@sha256:" in value, "release_digest")
        repository, sha = value.split("@sha256:", 1)
        image_repository(repository)
        require(matches(HEX, sha), "release_digest")
    require(release["frontend"].split("@")[0] != release["backend"].split("@")[0], "distinct_images")
    require(isinstance(release["files"], dict) and set(release["files"]) == set(FILES), "release_files")
    require(all(matches(HEX, value) for value in release["files"].values()), "release_file_hash")
    return release


def validate_directory(directory: Path) -> dict:
    expected = {"release.json", *FILES}
    require({p.name for p in directory.iterdir()} == expected, "artifact_paths")
    require(all(p.is_file() and not p.is_symlink() for p in directory.iterdir()), "artifact_file_type")
    release = validate_release(read_json((directory / "release.json").read_bytes()))
    for name, sha in release["files"].items():
        require(digest((directory / name).read_bytes()) == sha, "artifact_file_hash")
    return release


def envelope(release: dict, gates_sha256: str) -> str:
    validate_release(release)
    require(matches(HEX, gates_sha256), "owner_gates_hash")
    value = base64.b64encode(canonical({"release": release, "gates_sha256": gates_sha256})).decode()
    require(len(value) <= 4096, "ssm_parameter_size")
    return value


def decode_envelope(value: str) -> dict:
    require(matches(r"[A-Za-z0-9+/=]{1,4096}", value), "ssm_parameter")
    data = read_json(base64.b64decode(value, validate=True))
    require(set(data) == {"release", "gates_sha256"} and matches(HEX, data["gates_sha256"]), "envelope")
    validate_release(data["release"])
    return data
