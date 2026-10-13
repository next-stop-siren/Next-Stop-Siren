"""Registration HTTP contracts, Origin rejection and CORS preflight."""

from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError

from app.api.auth import registration_session
from app.main import create_app
from app.schemas.auth import PublicUser
from app.security.session_values import create_csrf_token
from app.security.settings import AuthSettings
from app.security.tokens import issue_access_token, issue_refresh_token
from app.services.registration import LocalEmailExists, RegistrationResult, RegistrationUnavailable

PATH = "/api/auth/register"
ORIGIN = "http://127.0.0.1:5173"
INPUT = {"email": "fixture@example.test", "password": "fixture-only-password"}


@pytest.fixture
def register_client(auth_settings):
    application = create_app(auth_settings)
    session = Mock()
    application.dependency_overrides[registration_session] = lambda: session
    now = datetime.now(timezone.utc)
    result = RegistrationResult(
        user=PublicUser(id="9007199254740993", email=INPUT["email"], created_at=now),
        access=issue_access_token(9007199254740993, auth_settings, now=now),
        refresh=issue_refresh_token(9007199254740993, auth_settings, now=now),
    )
    with patch("app.api.auth.register_local_user", return_value=result) as service:
        with TestClient(application, base_url="http://127.0.0.1:8000") as client:
            yield client, service, result


@pytest.fixture
def frontend_and_swagger_register_client(auth_environment):
    auth_environment.update(
        {
            "AUTH_API_ORIGIN": "http://127.0.0.1:8000",
            "AUTH_ALLOWED_ORIGINS": "http://127.0.0.1:5173,http://127.0.0.1:8000",
        }
    )
    settings = AuthSettings.from_env(auth_environment)
    application = create_app(settings)
    session = Mock()
    application.dependency_overrides[registration_session] = lambda: session
    now = datetime.now(timezone.utc)
    result = RegistrationResult(
        user=PublicUser(id="9007199254740993", email=INPUT["email"], created_at=now),
        access=issue_access_token(9007199254740993, settings, now=now),
        refresh=issue_refresh_token(9007199254740993, settings, now=now),
    )
    with patch("app.api.auth.register_local_user", return_value=result) as service:
        with TestClient(application, base_url="http://127.0.0.1:8000") as client:
            yield client, service


def assert_error(response, status, code):
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"code", "message", "fields", "trace_id"}
    assert error["code"] == code
    UUID(error["trace_id"])
    assert "set-cookie" not in response.headers
    assert INPUT["password"] not in response.text
    return error


def test_success_returns_public_body_and_matching_cookies(register_client, auth_settings):
    client, service, result = register_client
    response = client.post(PATH, json=INPUT, headers={"Origin": ORIGIN})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"user", "access_token", "token_type", "expires_in", "csrf_token"}
    assert body["user"]["id"] == "9007199254740993"
    assert set(body["user"]) == {"id", "email", "created_at"}
    assert body["user"]["created_at"].endswith("Z")
    assert body["access_token"] == result.access.token
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    assert body["csrf_token"] == create_csrf_token(result.refresh.jti, auth_settings)
    assert response.cookies["csrf_token"] == body["csrf_token"]
    assert response.cookies["refresh_token"] == result.refresh.token
    assert result.refresh.token not in response.text
    assert INPUT["password"] not in response.text
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for cookie in cookies:
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api/auth" in cookie
        assert "Domain=" not in cookie and "Secure" not in cookie
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "Origin" in response.headers["vary"]
    service.assert_called_once()


def test_registration_cors_and_insecure_cookies_for_explicit_loopback_origins(frontend_and_swagger_register_client):
    client, service = frontend_and_swagger_register_client
    for origin in ("http://127.0.0.1:5173", "http://127.0.0.1:8000"):
        preflight = client.options(
            PATH,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        assert preflight.headers["access-control-allow-credentials"] == "true"
        response = client.post(PATH, json=INPUT, headers={"Origin": origin})
        assert response.status_code == 201
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers["access-control-allow-credentials"] == "true"
        cookies = response.headers.get_list("set-cookie")
        assert len(cookies) == 2
        assert all("HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie for cookie in cookies)
    for origin in ("http://127.0.0.1:5174", "http://localhost:5173", "http://localhost:8000"):
        rejected = client.post(PATH, json=INPUT, headers={"Origin": origin})
        assert_error(rejected, 403, "csrf_failed")
        assert "access-control-allow-origin" not in rejected.headers
    assert service.call_count == 2


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "null",
        "http://127.0.0.1:5173/",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:8000",
        "https://127.0.0.1:5173",
        "http://127.0.0.1:5173.evil.test",
        "http://127.0.0.1:5173, https://evil.test",
    ],
)
def test_origin_must_match_exactly_before_service_runs(register_client, origin):
    client, service, _ = register_client
    headers = {} if origin is None else {"Origin": origin}
    response = client.post(PATH, json=INPUT, headers=headers)
    assert_error(response, 403, "csrf_failed")
    assert "access-control-allow-origin" not in response.headers
    service.assert_not_called()


def test_duplicate_origin_headers_are_rejected(register_client):
    client, service, _ = register_client
    response = client.post(PATH, json=INPUT, headers=[("Origin", ORIGIN), ("Origin", ORIGIN)])
    assert_error(response, 403, "csrf_failed")
    service.assert_not_called()


@pytest.mark.parametrize(
    "body", [{}, {"email": INPUT["email"], "password": "short"}, {**INPUT, "password_hash": "private-value"}]
)
def test_invalid_input_does_not_call_service(register_client, body):
    client, service, _ = register_client
    response = client.post(PATH, json=body, headers={"Origin": ORIGIN})
    assert_error(response, 422, "validation_error")
    assert "private-value" not in response.text
    assert response.headers["access-control-allow-origin"] == ORIGIN
    service.assert_not_called()


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (LocalEmailExists("private email"), 409, "local_email_exists"),
        (RegistrationUnavailable("private storage error"), 503, "temporarily_unavailable"),
    ],
)
def test_service_failures_return_safe_errors_without_cookies(register_client, failure, status, code):
    client, service, _ = register_client
    service.side_effect = failure
    response = client.post(PATH, json=INPUT, headers={"Origin": ORIGIN})
    assert_error(response, status, code)
    assert "private" not in response.text
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_preflight_allows_explicit_origin_method_and_headers(register_client):
    client, service, _ = register_client
    response = client.options(
        PATH,
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "set-cookie" not in response.headers
    service.assert_not_called()


@pytest.mark.parametrize(
    "origin,method,header",
    [
        ("https://evil.test", "POST", "content-type"),
        (ORIGIN, "DELETE", "content-type"),
        (ORIGIN, "POST", "x-unapproved-header"),
    ],
)
def test_preflight_rejects_unapproved_requests(register_client, origin, method, header):
    client, service, _ = register_client
    response = client.options(
        PATH,
        headers={"Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": header},
    )
    assert response.status_code == 400
    assert "set-cookie" not in response.headers
    if origin != ORIGIN:
        assert "access-control-allow-origin" not in response.headers
    service.assert_not_called()


def test_missing_auth_settings_block_signup_but_keep_health(monkeypatch):
    monkeypatch.delenv("AUTH_ACCESS_KEY", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        response = client.post(PATH, json=INPUT, headers={"Origin": ORIGIN})
        assert_error(response, 503, "temporarily_unavailable")
        assert "AUTH_ACCESS_KEY" not in response.text


@pytest.mark.parametrize("origin", ["https://app.example.test", "https://preview.example.test"])
def test_production_cookies_are_secure_for_each_explicit_origin(register_client, auth_environment, origin):
    auth_environment.update(
        {
            "AUTH_ENV": "production",
            "AUTH_COOKIE_SECURE": "true",
            "AUTH_API_ORIGIN": "https://api.example.test",
            "AUTH_ALLOWED_ORIGINS": "https://app.example.test,https://preview.example.test",
        }
    )
    application = create_app(AuthSettings.from_env(auth_environment))
    application.dependency_overrides[registration_session] = lambda: Mock()
    with TestClient(application, base_url="https://api.example.test") as client:
        response = client.post(PATH, json=INPUT, headers={"Origin": origin})
    assert response.status_code == 201
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert len(response.headers.get_list("set-cookie")) == 2
    for cookie in response.headers.get_list("set-cookie"):
        assert "Secure" in cookie and "HttpOnly" in cookie
        assert "Domain=" not in cookie


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("DATABASE_URL is missing"), OperationalError("private SQL", None, RuntimeError("private details"))],
)
def test_session_initialization_failure_is_safe(auth_settings, failure):
    def failing_session():
        raise failure
        yield  # 세션 생성 전 실패를 재현한다.

    with patch("app.api.auth.get_session", failing_session):
        with TestClient(create_app(auth_settings)) as client:
            response = client.post(PATH, json=INPUT, headers={"Origin": ORIGIN})
    assert_error(response, 503, "temporarily_unavailable")
    assert "private" not in response.text and "DATABASE_URL" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {**INPUT, "email": "invalid-email"},
        {**INPUT, "email": 123},
        {**INPUT, "password": None},
        {**INPUT, "password": "a" * 11},
        {**INPUT, "password": "a" * 129},
        {**INPUT, "user_id": "9007199254740993"},
    ],
)
def test_input_boundaries_are_rejected_through_signup_http(register_client, body):
    client, service, _ = register_client
    response = client.post(PATH, json=body, headers={"Origin": ORIGIN})
    error = assert_error(response, 422, "validation_error")
    assert error["fields"]
    assert "invalid-email" not in response.text and "9007199254740993" not in response.text
    service.assert_not_called()


def test_malformed_json_does_not_expose_submitted_password(register_client):
    client, service, _ = register_client
    response = client.post(
        PATH,
        content='{"email":"fixture@example.test","password":"private-broken-json"',
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    error = assert_error(response, 422, "validation_error")
    assert error["fields"] == {"body": "올바른 JSON 본문을 보내 주세요."}
    assert "private-broken-json" not in response.text
    service.assert_not_called()


def test_origin_is_checked_before_opening_a_database_session(auth_settings):
    with patch("app.api.auth.get_session") as sessions:
        with TestClient(create_app(auth_settings)) as client:
            response = client.post(PATH, json=INPUT, headers={"Origin": "https://evil.test"})
    assert_error(response, 403, "csrf_failed")
    sessions.assert_not_called()
