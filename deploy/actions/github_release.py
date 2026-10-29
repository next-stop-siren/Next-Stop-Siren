"""Read GitHub provenance and checksum-bound artifacts. No mutation methods."""

import io
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit
import zipfile

from contract import CI_WORKFLOW, FILES, OWNER, PUBLISH_WORKFLOW, REPOSITORY, digest, matches, read_json, require
from contract import validate_directory


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def get(self, path: str) -> dict:
        request = urllib.request.Request(
            "https://api.github.com/" + path,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "next-stop-siren-pilot",
            },
        )
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            return read_json(response.read(2_000_000))

    def artifact_zip(self, artifact_id: int) -> bytes:
        # Never forward the GitHub token to the signed artifact-storage redirect.
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip",
            headers={"Authorization": "Bearer " + self.token, "User-Agent": "next-stop-siren-pilot"},
        )
        try:
            urllib.request.build_opener(NoRedirect).open(request, timeout=30)
        except urllib.error.HTTPError as error:
            require(error.code == 302, "artifact_redirect")
            location = error.headers["Location"]
        else:
            raise ValueError("artifact_redirect_required")
        parsed = urlsplit(location)
        require(parsed.scheme == "https" and parsed.hostname and not parsed.username, "artifact_https")
        with urllib.request.urlopen(location, timeout=30) as response:
            data = response.read(1_000_001)
        require(len(data) <= 1_000_000, "artifact_size")
        return data


def authorized_context(values=os.environ) -> dict:
    require(values.get("GITHUB_REPOSITORY") == REPOSITORY, "repository")
    require(
        values.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and values.get("GITHUB_REF") == "refs/heads/main",
        "manual_main_only",
    )
    require(values.get("GITHUB_ACTOR") == OWNER and values.get("GITHUB_TRIGGERING_ACTOR") == OWNER, "owner_rerun")
    result = {}
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY_ID", "GITHUB_REPOSITORY_OWNER_ID"):
        value = values.get(name, "")
        require(matches(r"[1-9][0-9]*", value), "github_context_identity")
        result[name] = value
    require(matches(r"[0-9a-f]{40}", values.get("GITHUB_SHA")), "github_workflow_sha")
    result["GITHUB_SHA"] = values["GITHUB_SHA"]
    return result


def workflow_run(client, run: dict, path: str, event: str, sha: str, repo: dict) -> None:
    workflow = client.get(f"repos/{REPOSITORY}/actions/workflows/{quote(path.rsplit('/', 1)[1])}")
    require(run["workflow_id"] == workflow["id"] and run.get("path") == path, "workflow_identity")
    require(run["event"] == event and run["head_branch"] == "main" and run["head_sha"] == sha, "run_source")
    require(run["head_repository"]["full_name"] == REPOSITORY and run["head_repository"]["id"] == repo["id"], "fork")
    require(run["repository"]["id"] == repo["id"], "run_repository")


def environment_guard(client, name: str) -> None:
    policy = client.get(f"repos/{REPOSITORY}/environments/{name}")["deployment_branch_policy"]
    require(policy and policy["custom_branch_policies"] and not policy["protected_branches"], "environment_policy")
    response = client.get(f"repos/{REPOSITORY}/environments/{name}/deployment-branch-policies")
    policies = response["branch_policies"]
    require(response["total_count"] == 1, "environment_policy_count")
    require(
        len(policies) == 1 and policies[0]["name"] == "main" and policies[0].get("type", "branch") == "branch",
        "environment_main_only",
    )


def publication_guard(client, sha: str, context: dict) -> tuple[dict, int]:
    require(matches(r"[0-9a-f]{40}", sha), "source_sha")
    require(sha == context["GITHUB_SHA"], "workflow_source_sha")
    environment_guard(client, "pilot-publication")
    repo = client.get(f"repos/{REPOSITORY}")
    require(
        str(repo["id"]) == context["GITHUB_REPOSITORY_ID"]
        and str(repo["owner"]["id"]) == context["GITHUB_REPOSITORY_OWNER_ID"],
        "repository_ids",
    )
    require(client.get(f"repos/{REPOSITORY}/git/ref/heads/main")["object"]["sha"] == sha, "exact_main_sha")
    ci = client.get(f"repos/{REPOSITORY}/actions/workflows/ci.yml")
    runs = client.get(
        f"repos/{REPOSITORY}/actions/workflows/{ci['id']}/runs?head_sha={sha}&event=push&status=completed"
    )["workflow_runs"]
    success = False
    for run in runs:
        if run.get("status") == "completed" and run.get("conclusion") == "success":
            try:
                workflow_run(client, run, CI_WORKFLOW, "push", sha, repo)
                success = True
            except (ValueError, KeyError):
                pass
    require(success, "same_sha_trusted_ci_success_required")
    workflow = client.get(f"repos/{REPOSITORY}/actions/workflows/publish-pilot.yml")
    return repo, workflow["id"]


def download_release(client, run_id: str, directory: Path) -> dict:
    require(matches(r"[1-9][0-9]*", run_id), "publication_run_id")
    repo = client.get(f"repos/{REPOSITORY}")
    run = client.get(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    sha = run["head_sha"]
    workflow_run(client, run, PUBLISH_WORKFLOW, "workflow_dispatch", sha, repo)
    require(run["status"] == "completed" and run["conclusion"] == "success", "publication_success")
    require(run["actor"]["login"] == OWNER and run["triggering_actor"]["login"] == OWNER, "publication_owner")
    require(client.get(f"repos/{REPOSITORY}/compare/{sha}...main")["status"] in {"ahead", "identical"}, "main_ancestor")
    name = f"pilot-release-{sha}-{run_id}-{run['run_attempt']}"
    artifacts = client.get(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts")["artifacts"]
    selected = [artifact for artifact in artifacts if artifact["name"] == name and not artifact["expired"]]
    require(len(selected) == 1, "unique_artifact")
    artifact = selected[0]
    require(
        artifact["workflow_run"]["id"] == int(run_id) and artifact["workflow_run"]["head_sha"] == sha, "artifact_run"
    )
    data = client.artifact_zip(artifact["id"])
    require(artifact.get("digest") == "sha256:" + digest(data), "artifact_archive_digest")
    directory.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and set(names) == {"release.json", *FILES}, "artifact_paths")
        for item in archive.infolist():
            require(not stat.S_ISLNK(item.external_attr >> 16) and item.file_size <= 250_000, "artifact_type_size")
            (directory / item.filename).write_bytes(archive.read(item))
    release = validate_directory(directory)
    require(
        release["source_sha"] == sha
        and release["run_id"] == run_id
        and release["run_attempt"] == str(run["run_attempt"])
        and str(run["workflow_id"]) == release["workflow_id"]
        and str(repo["id"]) == release["repository_id"]
        and str(repo["owner"]["id"]) == release["owner_id"],
        "artifact_release_identity",
    )
    return release
