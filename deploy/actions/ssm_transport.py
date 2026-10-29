"""Pinned SSM dispatch once, bounded result polling, and redacted failures."""

import os
import subprocess
import time

from contract import HEX, canonical, decode_envelope, matches, read_json, require


class InvocationPending(Exception):
    pass


class DispatchUnknown(RuntimeError):
    """Never retry send-command after an ambiguous response."""


def configuration(values=os.environ) -> dict:
    names = {
        "account": "PILOT_AWS_ACCOUNT_ID",
        "role": "PILOT_AWS_ROLE_ARN",
        "region": "PILOT_AWS_REGION",
        "instance": "PILOT_INSTANCE_ID",
        "document": "PILOT_SSM_DOCUMENT_NAME",
        "version": "PILOT_SSM_DOCUMENT_VERSION",
        "document_hash": "PILOT_SSM_DOCUMENT_SHA256",
        "gates_hash": "PILOT_GATES_SHA256",
    }
    config = {key: values.get(name, "") for key, name in names.items()}
    require(matches(r"[0-9]{12}", config["account"]), "aws_account_required")
    require(matches(r"arn:aws:iam::" + config["account"] + r":role/[A-Za-z0-9_+=,.@/-]+", config["role"]), "aws_role")
    require(config["region"] == "ap-northeast-2", "explicit_seoul_region_required")
    require(matches(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", config["instance"]), "instance")
    require(
        matches(r"[A-Za-z][A-Za-z0-9_.-]{2,127}", config["document"]) and not config["document"].startswith("AWS-"),
        "controlled_document_required",
    )
    require(matches(r"[1-9][0-9]*", config["version"]), "numeric_pinned_document_version")
    require(matches(HEX, config["document_hash"]) and matches(HEX, config["gates_hash"]), "required_hashes")
    return config


class AwsCLI:
    def __init__(self, region: str):
        self.region = region

    def call(self, operation: str, arguments: list[str]) -> dict:
        env = {**os.environ, "AWS_MAX_ATTEMPTS": "1", "AWS_RETRY_MODE": "standard", "AWS_PAGER": ""}
        command = [
            "aws",
            "ssm",
            operation,
            "--region",
            self.region,
            "--output",
            "json",
            "--cli-connect-timeout",
            "10",
            "--cli-read-timeout",
            "30",
            *arguments,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, env=env)
        if result.returncode:
            if operation == "get-command-invocation" and "(InvocationDoesNotExist)" in result.stderr:
                raise InvocationPending()
            raise RuntimeError("aws_operation_failed")  # Never expose raw AWS/command stdout or stderr.
        return read_json(result.stdout)


def dispatch(
    client,
    config: dict,
    encoded: str,
    document: dict,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    timeout_seconds: float = 1200,
) -> str:
    decode_envelope(encoded)
    description = client.call(
        "describe-document", ["--name", config["document"], "--document-version", config["version"]]
    )["Document"]
    require(
        description["Hash"] == config["document_hash"]
        and description["HashType"] == "Sha256"
        and description["DocumentVersion"] == config["version"]
        and description["Name"] == config["document"]
        and description["Owner"] == config["account"]
        and description["Status"] == "Active"
        and description["DocumentType"] == "Command",
        "pinned_document_description",
    )
    observed = client.call(
        "get-document",
        ["--name", config["document"], "--document-version", config["version"], "--document-format", "JSON"],
    )
    require(
        observed["DocumentVersion"] == config["version"]
        and observed["Name"] == config["document"]
        and observed["Status"] == "Active"
        and observed["DocumentType"] == "Command"
        and canonical(read_json(observed["Content"])) == canonical(document),
        "pinned_document_content",
    )
    try:
        sent = client.call(
            "send-command",
            [
                "--instance-ids",
                config["instance"],
                "--document-name",
                config["document"],
                "--document-version",
                config["version"],
                "--document-hash",
                config["document_hash"],
                "--document-hash-type",
                "Sha256",
                "--timeout-seconds",
                "120",
                "--parameters",
                canonical({"ReleaseManifest": [encoded]}).decode(),
            ],
        )
        command_id = sent["Command"]["CommandId"]
        require(matches(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", command_id), "command_id")
    except Exception:
        raise DispatchUnknown("send_outcome_unknown_do_not_resend") from None
    print("SSM_COMMAND_ACCEPTED: " + command_id, flush=True)
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        try:
            result = client.call(
                "get-command-invocation", ["--command-id", command_id, "--instance-id", config["instance"]]
            )
        except InvocationPending:
            sleep(min(3, max(0, deadline - clock())))
            continue
        status = result.get("Status")
        if status == "Success":
            require(result.get("ResponseCode") == 0, "remote_nonzero")
            return command_id
        require(status in {"Pending", "InProgress", "Delayed"}, "remote_failed")
        sleep(min(3, max(0, deadline - clock())))
    raise TimeoutError("poll_timeout_remote_may_still_run")
