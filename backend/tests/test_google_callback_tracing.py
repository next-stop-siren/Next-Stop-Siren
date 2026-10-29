"""Correlate callback receipt, validation failures, and responses without exposing credentials."""

from datetime import datetime, timezone
import logging
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.observability import FailureDetails
from app.schemas.auth import PublicUser
from app.security.tokens import issue_refresh_token
from app.services.google_sessions import GoogleLoginResult
from app.services.login import LoginUnavailable
from google_helpers import ACCESS_TOKEN

PATH = "/api/auth/google/callback"
RECEIVED = "google_callback_received"
REJECTED = "google_callback_verification_failed"
FAILURE = "google_login_failure"


def app_events(caplog):
    return [record for record in caplog.records if record.name.startswith("app.") and "event=" in record.getMessage()]


def event(record):
    return record.getMessage().split()[0].removeprefix("event=")


def trace_id(record):
    return record.getMessage().split("trace_id=", 1)[1].split()[0]


def assert_safe(caplog, response, flow):
    secrets = [
        flow.params["code"],
        flow.params["state"],
        flow.cookie,
        flow.transaction.nonce,
        flow.transaction.code_verifier,
        flow.settings.client_secret,
        ACCESS_TOKEN,
        "fixture-client-trace",
        "fixture-private-description",
        "fixture-private-error",
        "fixture@example.test",
    ]
    if hasattr(flow, "id_token"):
        secrets.append(flow.id_token)
    for secret in secrets:
        assert secret not in caplog.text and secret not in response.text
        assert secret not in response.headers.get("location", "")
    assert all(record.exc_info is None and record.stack_info is None for record in app_events(caplog))


@pytest.mark.parametrize(
    "kind", ["missing-cookie", "bad-state", "duplicate-state", "cancelled", "nonce", "email", "invalid-grant"]
)
def test_verification_failure_links_received_failed_and_response_ids(google_callback_flow, caplog, kind):
    flow = google_callback_flow
    params = dict(flow.params)
    if kind == "missing-cookie":
        flow.client.cookies.clear()
    elif kind == "bad-state":
        params["state"] = "fixture-private-state"
    elif kind == "duplicate-state":
        params = [*params.items(), ("state", "fixture-private-state")]
    elif kind == "cancelled":
        params.pop("code")
        params.update(error="fixture-private-error", error_description="fixture-private-description")
    elif kind == "nonce":
        flow.behavior["claims"] = {"nonce": "fixture-private-nonce"}
    elif kind == "email":
        flow.behavior["claims"] = {"email_verified": False}
    else:
        flow.behavior["exchange_error"] = "invalid_grant"
    with caplog.at_level(logging.INFO), patch("app.api.google_auth.login_session") as opened:
        response = flow.client.get(PATH, params=params, headers={"X-Trace-ID": "fixture-client-trace"})
    assert response.status_code == 302
    assert response.headers["location"] == "/?google_login_error=invalid_request"
    records = app_events(caplog)
    assert [event(record) for record in records] == [RECEIVED, REJECTED, FAILURE]
    response_id = trace_id(records[0])
    assert str(UUID(response_id)) == response_id
    assert [trace_id(record) for record in records] == [response_id] * 3
    assert records[0].levelno == logging.INFO and records[1].levelno == logging.WARNING
    opened.assert_not_called()
    assert_safe(caplog, response, flow)
    for private in ("fixture-private-state", "fixture-private-nonce"):
        assert private not in caplog.text


def test_different_callbacks_get_different_server_ids(google_callback_flow, caplog):
    flow = google_callback_flow
    ids = []
    with caplog.at_level(logging.INFO), patch("app.api.google_auth.login_session") as opened:
        for _ in range(2):
            caplog.clear()
            response = flow.client.get(PATH, headers={"X-Trace-ID": "fixture-client-trace"})
            assert response.status_code == 302
            records = app_events(caplog)
            current = trace_id(records[0])
            assert [event(record) for record in records] == [RECEIVED, REJECTED, FAILURE]
            assert [trace_id(record) for record in records] == [current] * 3
            ids.append(current)
    assert len(set(ids)) == 2 and "fixture-client-trace" not in ids
    opened.assert_not_called()


@pytest.mark.parametrize(
    "kind,stage", [("settings", "google_settings"), ("provider", "google_discovery"), ("storage", "commit")]
)
def test_service_failure_uses_received_trace_without_claiming_verification_failure(
    google_callback_flow, caplog, kind, stage
):
    flow = google_callback_flow
    if kind == "settings":
        flow.app.state.google_settings = None
    elif kind == "provider":
        flow.behavior["discovery_failure"] = True
    failure = LoginUnavailable(failure=FailureDetails(stage="commit", error_type="OperationalError"))

    def sessions():
        yield MagicMock(spec=Session)

    with (
        caplog.at_level(logging.INFO),
        patch("app.api.google_auth.login_session", side_effect=sessions),
        patch("app.api.google_auth.create_google_login_session", side_effect=failure),
    ):
        response = flow.client.get(PATH, params=flow.params)
    assert response.status_code == (503 if kind == "settings" else 302)
    records = app_events(caplog)
    assert [event(record) for record in records] == [RECEIVED, "authentication_failed", FAILURE]
    assert len({trace_id(record) for record in records}) == 1
    if kind == "settings":
        assert trace_id(records[0]) == response.json()["error"]["trace_id"]
    else:
        assert response.headers["location"] == "/?google_login_error=temporarily_unavailable"
    assert f"stage={stage}" in records[1].getMessage()
    assert_safe(caplog, response, flow)


def test_successful_callback_does_not_emit_verification_failure(google_callback_flow, caplog):
    flow = google_callback_flow
    settings = flow.app.state.auth_settings
    result = GoogleLoginResult(
        user=PublicUser(id="101", email="fixture@example.test", created_at=datetime.now(timezone.utc)),
        refresh=issue_refresh_token(101, settings),
    )

    def sessions():
        yield MagicMock(spec=Session)

    with (
        caplog.at_level(logging.INFO),
        patch("app.api.google_auth.login_session", side_effect=sessions),
        patch("app.api.google_auth.create_google_login_session", return_value=result),
    ):
        response = flow.client.get(PATH, params=flow.params)
    assert response.status_code == 302
    records = app_events(caplog)
    assert [event(record) for record in records] == [RECEIVED, "login_saved"]
    assert trace_id(records[0]) == trace_id(records[1])
    assert result.refresh.token not in caplog.text
    assert_safe(caplog, response, flow)
