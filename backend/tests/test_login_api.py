"""Login HTTP contracts, failure limits, compensation, and safe logging."""

from datetime import datetime, timezone
from contextlib import nullcontext
import logging
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.auth import login_session
from app.api.cookies import AuthCookieError
from app.main import create_app
from app.models.user import User
from app.security.login_limits import LoginFailureLimiter
from app.security.settings import AuthSettings
from app.security.tokens import TokenIssueError
from app.services.login import InvalidCredentials, LoginUnavailable

PATH = "/api/auth/login"
BODY = {"email": "fixture@example.test", "password": "fixture-only-password"}
ORIGIN = "http://127.0.0.1:5173"
HEADERS = {"Origin": ORIGIN}


@pytest.fixture
def login_client(auth_settings):
    app = create_app(auth_settings)
    clock = [0.0]
    app.state.login_failure_limiter = LoginFailureLimiter(clock=lambda: clock[0])
    session = Mock(spec=Session)
    app.dependency_overrides[login_session] = lambda: session
    user = User(
        id=9007199254740993, email=BODY["email"], password_hash="private-hash", created_at=datetime.now(timezone.utc)
    )
    with (
        patch("app.services.login.local_email_key", side_effect=lambda session, email: email.lower()),
        patch("app.services.login.authenticate_local_user", return_value=user) as authenticate,
        patch("app.services.login.find_user_for_update", return_value=user),
        patch("app.services.login.add_initial_refresh_session") as insert,
        TestClient(app, base_url="http://127.0.0.1:8000", client=("192.0.2.1", 123)) as client,
    ):
        yield client, session, authenticate, insert, user, clock


def assert_error(response, status: int, code: str):
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["fields"] == {}
    assert response.json()["error"]["trace_id"]
    assert "set-cookie" not in response.headers
    assert BODY["password"] not in response.text and "private" not in response.text
    return response.json()["error"]


def test_login_commits_before_setting_cookies_and_returns_public_contract(login_client, auth_settings) -> None:
    client, session, _, insert, _, _ = login_client
    from app.api.cookies import set_auth_cookies

    def cookies(response, refresh, settings):
        session.commit.assert_called_once()
        return set_auth_cookies(response, refresh, settings)

    with patch("app.api.auth.set_auth_cookies", side_effect=cookies):
        response = client.post(PATH, json=BODY, headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"user", "access_token", "token_type", "expires_in", "csrf_token"}
    assert data["user"]["id"] == "9007199254740993"
    assert set(data["user"]) == {"id", "email", "created_at"}
    assert data["token_type"] == "Bearer" and data["expires_in"] == 900
    assert data["csrf_token"] == response.cookies["csrf_token"]
    assert response.cookies["refresh_token"] not in response.text
    assert "private-hash" not in response.text
    assert response.headers["cache-control"] == "no-store" and response.headers["pragma"] == "no-cache"
    assert response.headers["access-control-allow-origin"] == ORIGIN
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api/auth" in cookie for cookie in cookies)
    assert all("Domain=" not in cookie and "Secure" not in cookie for cookie in cookies)
    assert insert.call_args.args[1] == 9007199254740993
    session.add.assert_not_called()


def test_missing_and_wrong_password_failures_have_same_http_body(login_client) -> None:
    client, session, authenticate, insert, _, _ = login_client
    authenticate.side_effect = InvalidCredentials()
    responses = [
        client.post(PATH, json={**BODY, "email": email}, headers=HEADERS)
        for email in (BODY["email"], "missing@example.test")
    ]
    bodies = [assert_error(response, 401, "unauthenticated") for response in responses]
    assert {key: value for key, value in bodies[0].items() if key != "trace_id"} == {
        key: value for key, value in bodies[1].items() if key != "trace_id"
    }
    insert.assert_not_called()
    session.commit.assert_not_called()


def test_actual_login_route_limits_failures_and_allows_retry_after_window(login_client) -> None:
    client, _, authenticate, insert, _, clock = login_client
    authenticate.side_effect = InvalidCredentials()
    for _ in range(5):
        assert_error(client.post(PATH, json=BODY, headers=HEADERS), 401, "unauthenticated")
    authenticate.side_effect = None
    response = client.post(PATH, json=BODY, headers=HEADERS)
    assert_error(response, 429, "rate_limited")
    assert response.headers["retry-after"] == "600"
    insert.assert_not_called()
    clock[0] = 600
    assert client.post(PATH, json=BODY, headers=HEADERS).status_code == 200


@pytest.mark.parametrize("failure_kind", ["commit", "token", "cookie"])
def test_failed_login_does_not_reset_previous_email_failures(login_client, failure_kind: str) -> None:
    client, session, authenticate, _, _, _ = login_client
    authenticate.side_effect = InvalidCredentials()
    for _ in range(4):
        assert client.post(PATH, json=BODY, headers=HEADERS).status_code == 401
    authenticate.side_effect = None
    if failure_kind == "commit":
        session.commit.side_effect = OperationalError("private SQL", None, RuntimeError("private"))
        failure = nullcontext()
    elif failure_kind == "token":
        failure = patch("app.services.login.issue_access_token", side_effect=TokenIssueError("private token"))
    else:
        failure = patch("app.api.auth.set_auth_cookies", side_effect=AuthCookieError("private cookie"))
    with failure, patch("app.api.auth.cancel_login"):
        assert_error(client.post(PATH, json=BODY, headers=HEADERS), 503, "temporarily_unavailable")
    session.commit.side_effect = None
    authenticate.side_effect = InvalidCredentials()
    assert client.post(PATH, json=BODY, headers=HEADERS).status_code == 401
    assert client.post(PATH, json=BODY, headers=HEADERS).status_code == 429


def test_fully_successful_login_resets_email_failures(login_client) -> None:
    client, _, authenticate, _, _, _ = login_client
    authenticate.side_effect = InvalidCredentials()
    for _ in range(4):
        assert client.post(PATH, json=BODY, headers=HEADERS).status_code == 401
    authenticate.side_effect = None
    assert client.post(PATH, json=BODY, headers=HEADERS).status_code == 200
    authenticate.side_effect = InvalidCredentials()
    for _ in range(5):
        assert client.post(PATH, json=BODY, headers=HEADERS).status_code == 401


@pytest.mark.parametrize("locked", [None, User(id=9007199254740993, email=BODY["email"], password_hash="changed-hash")])
def test_deleted_or_changed_account_is_rejected_before_issuance(login_client, locked) -> None:
    client, session, _, insert, _, _ = login_client
    with patch("app.services.login.find_user_for_update", return_value=locked):
        assert_error(client.post(PATH, json=BODY, headers=HEADERS), 401, "unauthenticated")
    insert.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize("origin", [None, "https://evil.test", "http://localhost:5173"])
def test_origin_is_checked_before_session_creation(auth_settings, origin) -> None:
    with patch("app.api.auth.get_session") as sessions:
        with TestClient(create_app(auth_settings), client=("192.0.2.1", 123)) as client:
            assert_error(
                client.post(PATH, json=BODY, headers={} if origin is None else {"Origin": origin}), 403, "csrf_failed"
            )
    sessions.assert_not_called()


@pytest.mark.parametrize("body", [{}, {**BODY, "password": "short"}, {**BODY, "user_id": "202"}])
def test_invalid_input_never_creates_a_login_session(login_client, body) -> None:
    client, session, authenticate, insert, _, _ = login_client
    response = client.post(PATH, json=body, headers=HEADERS)
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error"
    authenticate.assert_not_called()
    insert.assert_not_called()
    session.commit.assert_not_called()


def test_partial_cookie_failure_has_safe_logs_and_attempts_session_compensation(login_client, caplog) -> None:
    client, _, _, _, _, _ = login_client

    def cookie_failure(response, refresh, settings):
        response.set_cookie("refresh_token", refresh.token)
        raise AuthCookieError("private cookie details")

    with (
        caplog.at_level(logging.INFO),
        patch("app.api.auth.set_auth_cookies", side_effect=cookie_failure),
        patch("app.api.auth.cancel_login") as cancel,
    ):
        error = assert_error(client.post(PATH, json=BODY, headers=HEADERS), 503, "temporarily_unavailable")
    cancel.assert_called_once()
    events = [record.getMessage() for record in caplog.records if "event=login_" in record.getMessage()]
    assert [event.split()[0] for event in events] == [
        "event=login_received",
        "event=login_saved",
        "event=login_cancelled",
        "event=login_failed",
    ]
    assert all(error["trace_id"] in event for event in events)
    assert BODY["email"] not in caplog.text and BODY["password"] not in caplog.text and "private" not in caplog.text


def test_compensation_failure_is_logged_without_claiming_success(login_client, caplog) -> None:
    client, _, _, _, _, _ = login_client
    with (
        caplog.at_level(logging.INFO),
        patch("app.api.auth.set_auth_cookies", side_effect=AuthCookieError("private")),
        patch("app.api.auth.cancel_login", side_effect=LoginUnavailable()),
    ):
        assert_error(client.post(PATH, json=BODY, headers=HEADERS), 503, "temporarily_unavailable")
    assert "event=login_compensation_failed" in caplog.text
    assert "event=login_cancelled" not in caplog.text
    assert "private" not in caplog.text


def test_session_initialization_failure_is_safe_and_correlated(auth_settings, caplog) -> None:
    def failing_session():
        raise OperationalError("private SQL", None, RuntimeError("private connection"))
        yield

    with patch("app.api.auth.get_session", failing_session), caplog.at_level(logging.INFO):
        with TestClient(create_app(auth_settings), client=("192.0.2.1", 123)) as client:
            error = assert_error(client.post(PATH, json=BODY, headers=HEADERS), 503, "temporarily_unavailable")
    events = [record.getMessage() for record in caplog.records if "event=login_" in record.getMessage()]
    assert len(events) == 2 and all(error["trace_id"] in event for event in events)
    assert "stage=session" in events[1]
    assert "private" not in caplog.text


def test_production_login_uses_secure_host_only_cookies(login_client, auth_environment) -> None:
    _, session, _, _, _, _ = login_client
    auth_environment.update(
        {
            "AUTH_ENV": "production",
            "AUTH_COOKIE_SECURE": "true",
            "AUTH_API_ORIGIN": "https://api.example.test",
            "AUTH_ALLOWED_ORIGINS": "https://app.example.test",
        }
    )
    app = create_app(AuthSettings.from_env(auth_environment))
    app.dependency_overrides[login_session] = lambda: session
    with TestClient(app, base_url="https://api.example.test", client=("192.0.2.1", 123)) as client:
        response = client.post(PATH, json=BODY, headers={"Origin": "https://app.example.test"})
    assert response.status_code == 200
    assert all(
        "Secure" in cookie and "HttpOnly" in cookie and "Domain=" not in cookie
        for cookie in response.headers.get_list("set-cookie")
    )


def test_missing_auth_settings_block_login_without_opening_session(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_ACCESS_KEY", raising=False)
    with patch("app.api.auth.get_session") as sessions:
        with TestClient(create_app(), client=("192.0.2.1", 123)) as client:
            assert_error(client.post(PATH, json=BODY, headers=HEADERS), 503, "temporarily_unavailable")
    sessions.assert_not_called()
