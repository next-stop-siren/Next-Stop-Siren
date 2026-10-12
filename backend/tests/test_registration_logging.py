"""Registration request correlation and secret-free failure logging through HTTP."""

from datetime import datetime, timedelta, timezone
import logging
from unittest.mock import Mock, patch
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

from app.api.auth import registration_session
from app.api.cookies import AuthCookieError
from app.main import create_app
from app.models.user import User
from app.security.passwords import PasswordHashError
from app.security.tokens import IssuedToken, TokenIssueError

PATH = "/api/auth/register"
INPUT = {"email": "private-email@example.test", "password": "private-fixture-password"}
SECRETS = (
    *INPUT.values(),
    "private-hash",
    "private-access-token",
    "private-refresh-token",
    "private-digest",
    "private-csrf",
    "private-db-password",
    "db.private.test",
    "private_table",
)
STATEMENT = "INSERT INTO private_table (password_hash, token_digest) VALUES (%(hash)s, %(digest)s)"
VALUES = {"hash": "private-hash", "digest": "private-digest", "token": "private-refresh-token"}


def storage_error(kind=OperationalError):
    return kind(STATEMENT, VALUES, RuntimeError("password=private-db-password host=db.private.test"))


def registration_events(caplog):
    return [
        record
        for record in caplog.records
        if record.name.startswith("app.") and "event=registration_" in record.getMessage()
    ]


def assert_no_secrets(caplog, response):
    for secret in SECRETS:
        assert secret not in caplog.text
        assert secret not in response.text
    assert all(record.exc_info is None and record.stack_info is None for record in registration_events(caplog))


@pytest.fixture
def real_registration_client(auth_settings):
    session = Mock()
    application = create_app(auth_settings)
    application.dependency_overrides[registration_session] = lambda: session
    user = User(id=101, email=INPUT["email"], password_hash="private-hash", created_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    access = IssuedToken("private-access-token", "access", "fixture-access-jti", now, now + timedelta(minutes=15))
    refresh = IssuedToken("private-refresh-token", "refresh", "fixture-refresh-jti", now, now + timedelta(days=14))
    with (
        patch("app.services.registration.hash_password", return_value="private-hash"),
        patch("app.services.registration.find_local_user_by_email", return_value=None),
        patch("app.services.registration.add_local_user", return_value=user),
        patch("app.services.registration.issue_access_token", return_value=access),
        patch("app.services.registration.issue_refresh_token", return_value=refresh),
        TestClient(application) as client,
    ):
        yield client, session


@pytest.mark.parametrize(
    "stage,target,kind",
    [
        ("email_lookup", "find_local_user_by_email", OperationalError),
        ("user_insert", "add_local_user", IntegrityError),
        ("refresh_session", "add_initial_refresh_session", OperationalError),
        ("commit", None, OperationalError),
        ("rollback", None, InterfaceError),
    ],
)
def test_storage_failures_keep_trace_and_safe_error_type(
    real_registration_client, auth_settings, caplog, stage, target, kind
):
    client, session = real_registration_client
    failure = storage_error(kind)
    if target is None:
        session.commit.side_effect = storage_error()
        if stage == "rollback":
            session.rollback.side_effect = failure
        failure_context = patch("app.services.registration.hash_password", return_value="private-hash")
    else:
        failure_context = patch(f"app.services.registration.{target}", side_effect=failure)
    with caplog.at_level(logging.INFO), failure_context:
        response = client.post(
            PATH, json=INPUT, headers={"Origin": auth_settings.allowed_origins[0], "X-Request-ID": "client-supplied-id"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "temporarily_unavailable"
    trace_id = response.json()["error"]["trace_id"]
    UUID(trace_id)
    assert trace_id != "client-supplied-id"
    received, failed = registration_events(caplog)
    assert "event=registration_received" in received.getMessage()
    assert "event=registration_failed" in failed.getMessage()
    assert trace_id in received.getMessage() and trace_id in failed.getMessage()
    assert f"stage={stage}" in failed.getMessage()
    assert f"error_type={kind.__name__}" in failed.getMessage()
    assert failed.levelno == logging.ERROR
    assert "set-cookie" not in response.headers
    assert_no_secrets(caplog, response)
    session.rollback.assert_called_once()


@pytest.mark.parametrize(
    "target,failure,stage",
    [
        ("hash_password", PasswordHashError("private-hash"), "password_hash"),
        ("issue_access_token", TokenIssueError("private-access-token"), "token_issue"),
    ],
)
def test_credential_failures_are_not_reported_as_database_writes(
    real_registration_client, auth_settings, caplog, target, failure, stage
):
    client, _ = real_registration_client
    with caplog.at_level(logging.INFO), patch(f"app.services.registration.{target}", side_effect=failure):
        response = client.post(PATH, json=INPUT, headers={"Origin": auth_settings.allowed_origins[0]})
    assert response.status_code == 503
    received, failed = registration_events(caplog)
    assert f"stage={stage}" in failed.getMessage()
    assert f"error_type={type(failure).__name__}" in failed.getMessage()
    assert response.json()["error"]["trace_id"] in received.getMessage()
    assert response.json()["error"]["trace_id"] in failed.getMessage()
    assert_no_secrets(caplog, response)


def test_saved_event_follows_commit_and_reuses_received_trace(real_registration_client, auth_settings, caplog):
    client, session = real_registration_client

    def commit():
        assert len(registration_events(caplog)) == 1
        assert "event=registration_received" in registration_events(caplog)[0].getMessage()

    session.commit.side_effect = commit
    with caplog.at_level(logging.INFO):
        response = client.post(PATH, json=INPUT, headers={"Origin": auth_settings.allowed_origins[0]})
    assert response.status_code == 201
    received, saved = registration_events(caplog)
    assert "event=registration_saved" in saved.getMessage()
    assert received.getMessage().split("trace_id=")[1] == saved.getMessage().split("trace_id=")[1]
    for secret in SECRETS:
        assert secret not in caplog.text
    assert response.json()["csrf_token"] not in caplog.text
    assert response.cookies["refresh_token"] not in caplog.text
    session.commit.assert_called_once()
    session.rollback.assert_not_called()


def test_validation_error_uses_the_received_trace(real_registration_client, auth_settings, caplog):
    client, session = real_registration_client
    with caplog.at_level(logging.INFO):
        response = client.post(
            PATH,
            content='{"password":"private-fixture-password",',
            headers={"Origin": auth_settings.allowed_origins[0], "Content-Type": "application/json"},
        )
    assert response.status_code == 422
    (received,) = registration_events(caplog)
    assert response.json()["error"]["trace_id"] in received.getMessage()
    assert_no_secrets(caplog, response)
    session.commit.assert_not_called()


def test_session_creation_failure_is_correlated_and_sanitized(auth_settings, caplog):
    def failing_session():
        raise storage_error()
        yield  # DB 세션 생성 전 실패를 재현한다.

    with caplog.at_level(logging.INFO), patch("app.api.auth.get_session", failing_session):
        with TestClient(create_app(auth_settings)) as client:
            response = client.post(PATH, json=INPUT, headers={"Origin": auth_settings.allowed_origins[0]})
    assert response.status_code == 503
    received, failed = registration_events(caplog)
    trace_id = response.json()["error"]["trace_id"]
    assert trace_id in received.getMessage() and trace_id in failed.getMessage()
    assert "stage=session error_type=OperationalError" in failed.getMessage()
    assert_no_secrets(caplog, response)


def test_cookie_failure_keeps_the_committed_storage_event(real_registration_client, auth_settings, caplog):
    client, session = real_registration_client
    with (
        caplog.at_level(logging.INFO),
        patch("app.api.auth.set_auth_cookies", side_effect=AuthCookieError("private-refresh-token")),
    ):
        response = client.post(PATH, json=INPUT, headers={"Origin": auth_settings.allowed_origins[0]})
    assert response.status_code == 503
    received, saved, failed = registration_events(caplog)
    trace_id = response.json()["error"]["trace_id"]
    assert all(trace_id in record.getMessage() for record in (received, saved, failed))
    assert "event=registration_saved" in saved.getMessage()
    assert "stage=cookie error_type=AuthCookieError" in failed.getMessage()
    assert "set-cookie" not in response.headers
    assert_no_secrets(caplog, response)
    session.commit.assert_called_once()


def test_each_signup_request_has_a_fresh_trace(real_registration_client, auth_settings, caplog):
    client, _ = real_registration_client
    with caplog.at_level(logging.INFO):
        responses = [client.post(PATH, json={}, headers={"Origin": auth_settings.allowed_origins[0]}) for _ in range(2)]
    traces = [response.json()["error"]["trace_id"] for response in responses]
    assert len(set(traces)) == 2
    assert len(registration_events(caplog)) == 2
    for response, record in zip(responses, registration_events(caplog), strict=True):
        assert response.status_code == 422
        assert response.json()["error"]["trace_id"] in record.getMessage()
