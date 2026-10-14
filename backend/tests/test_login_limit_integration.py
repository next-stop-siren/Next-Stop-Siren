"""Exercise limited credential checks and their HTTP error contract."""

from unittest.mock import Mock, patch

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.auth import request_client_ip, request_login_limiter, require_allowed_origin
from app.api.errors import ApiError, unauthenticated
from app.main import create_app
from app.models.user import User
from app.schemas.auth import LoginRequest
from app.security.login_limits import LoginFailureLimiter, LoginRateLimited
from app.services.login import (
    InvalidCredentials,
    LoginUnavailable,
    authenticate_limited_local_user,
)

ORIGIN = "http://127.0.0.1:5173"
BODY = {"email": "fixture@example.test", "password": "fixture-only-password"}
IP = "192.0.2.1"


@pytest.fixture
def limited_client(auth_settings):
    app = create_app(auth_settings)
    clock = [0.0]
    app.state.login_failure_limiter = LoginFailureLimiter(clock=lambda: clock[0])
    session = Mock(spec=Session)

    # 실제 로그인 API가 추가되기 전까지 시험용 경로로 제한과 공통 오류 처리를 확인한다.
    @app.post("/test/login", dependencies=[Depends(require_allowed_origin)])
    def login(body: LoginRequest, request: Request) -> dict[str, str]:
        try:
            user = authenticate_limited_local_user(
                body, session, request_login_limiter(request), request_client_ip(request)
            )
        except InvalidCredentials:
            raise unauthenticated() from None
        except LoginUnavailable:
            raise ApiError("temporarily_unavailable") from None
        return {"user_id": str(user.id)}

    with (
        patch("app.services.login.local_email_key", side_effect=lambda session, email: email.lower()) as key,
        patch("app.services.login.authenticate_local_user", side_effect=InvalidCredentials()) as verify,
        TestClient(app, client=(IP, 50000)) as client,
    ):
        yield client, verify, key, clock


def test_email_variants_share_limits_and_return_safe_429(limited_client) -> None:
    client, verify, key, clock = limited_client
    for index in range(5):
        email = BODY["email"] if index % 2 else " FIXTURE@EXAMPLE.TEST "
        response = client.post("/test/login", json={**BODY, "email": email}, headers={"Origin": ORIGIN})
        assert response.status_code == 401
        assert "retry-after" not in response.headers
    response = client.post("/test/login", json=BODY, headers={"Origin": ORIGIN})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "600"
    assert response.headers["access-control-expose-headers"] == "Retry-After"
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "set-cookie" not in response.headers
    error = response.json()["error"]
    assert set(error) == {"code", "message", "fields", "trace_id"}
    assert error["code"] == "rate_limited" and error["fields"] == {} and error["trace_id"]
    assert BODY["email"] not in response.text and BODY["password"] not in response.text and IP not in response.text
    assert verify.call_count == 5
    assert all(call.args[1] == call.args[1].strip() for call in key.call_args_list)
    clock[0] = 600
    assert client.post("/test/login", json=BODY, headers={"Origin": ORIGIN}).status_code == 401


def test_forwarded_headers_cannot_bypass_ip_limit(limited_client) -> None:
    client, verify, _, _ = limited_client
    for index in range(30):
        response = client.post(
            "/test/login",
            json={**BODY, "email": f"fixture-{index}@example.test"},
            headers={"Origin": ORIGIN, "X-Forwarded-For": f"198.51.100.{index + 1}"},
        )
        assert response.status_code == 401
    response = client.post("/test/login", json=BODY, headers={"Origin": ORIGIN, "Forwarded": "for=198.51.100.100"})
    assert response.status_code == 429
    assert verify.call_count == 30


def test_success_clears_email_failures(limited_client) -> None:
    client, verify, _, _ = limited_client
    for _ in range(4):
        assert client.post("/test/login", json=BODY, headers={"Origin": ORIGIN}).status_code == 401
    verify.side_effect = None
    verify.return_value = User(id=101, email=BODY["email"], password_hash="fixture-hash")
    response = client.post("/test/login", json=BODY, headers={"Origin": ORIGIN})
    assert response.status_code == 200 and response.json() == {"user_id": "101"}
    verify.side_effect = InvalidCredentials()
    for _ in range(5):
        assert client.post("/test/login", json=BODY, headers={"Origin": ORIGIN}).status_code == 401
    assert client.post("/test/login", json=BODY, headers={"Origin": ORIGIN}).status_code == 429


def test_internal_failures_do_not_consume_attempts(limited_client) -> None:
    client, verify, _, _ = limited_client
    verify.side_effect = LoginUnavailable()
    for _ in range(6):
        assert client.post("/test/login", json=BODY, headers={"Origin": ORIGIN}).status_code == 503
    verify.side_effect = InvalidCredentials()
    for _ in range(5):
        assert client.post("/test/login", json=BODY, headers={"Origin": ORIGIN}).status_code == 401


def test_invalid_input_and_origin_do_not_consume_attempts(limited_client) -> None:
    client, verify, key, _ = limited_client
    for _ in range(6):
        assert (
            client.post("/test/login", json={**BODY, "password": "short"}, headers={"Origin": ORIGIN}).status_code
            == 422
        )
        assert client.post("/test/login", json=BODY, headers={"Origin": "https://evil.test"}).status_code == 403
    verify.assert_not_called()
    key.assert_not_called()


def test_database_key_is_used_before_credential_lookup() -> None:
    limiter = LoginFailureLimiter()
    body = LoginRequest.model_validate(BODY)
    session = Mock(spec=Session)
    with (
        patch("app.services.login.local_email_key", return_value="db-comparison-key") as key,
        patch("app.services.login.authenticate_local_user", side_effect=InvalidCredentials()) as verify,
    ):
        for _ in range(5):
            with pytest.raises(InvalidCredentials):
                authenticate_limited_local_user(body, session, limiter, IP)
        with pytest.raises(LoginRateLimited):
            authenticate_limited_local_user(body, session, limiter, IP)
    key.assert_called_with(session, body.email)
    assert verify.call_count == 5
    with pytest.raises(LoginRateLimited):
        with limiter.attempt("db-comparison-key", "192.0.2.2"):
            pass


@pytest.mark.parametrize(
    "failure", [OperationalError("private SQL", None, RuntimeError("private")), ValueError("private")]
)
def test_email_key_failure_is_safe_and_skips_authentication(failure: Exception) -> None:
    with (
        patch("app.services.login.local_email_key", side_effect=failure),
        patch("app.services.login.authenticate_local_user") as verify,
    ):
        with pytest.raises(LoginUnavailable) as caught:
            authenticate_limited_local_user(LoginRequest.model_validate(BODY), Mock(), LoginFailureLimiter(), IP)
    assert "private" not in str(caught.value)
    verify.assert_not_called()


@pytest.mark.parametrize("peer", [None, ("testclient", 123), ("not-an-ip", 123)])
def test_missing_or_invalid_peer_ip_is_unavailable(peer) -> None:
    request = Request({"type": "http", "client": peer, "headers": [(b"x-forwarded-for", b"192.0.2.1")]})
    with pytest.raises(ApiError) as caught:
        request_client_ip(request)
    assert caught.value.code == "temporarily_unavailable"


def test_each_app_has_an_independent_limiter(auth_settings) -> None:
    first = create_app(auth_settings)
    second = create_app(auth_settings)
    first_request = Request({"type": "http", "app": first})
    second_request = Request({"type": "http", "app": second})
    assert request_login_limiter(first_request) is request_login_limiter(first_request)
    assert request_login_limiter(first_request) is not request_login_limiter(second_request)
    del first.state.login_failure_limiter
    with pytest.raises(ApiError) as caught:
        request_login_limiter(first_request)
    assert caught.value.code == "temporarily_unavailable"


@pytest.mark.parametrize("retry_after", [None, 0, -1, True, 1.5, "600\r\nSet-Cookie: private"])
def test_rate_limit_error_requires_positive_integer_wait(retry_after) -> None:
    with pytest.raises(ValueError):
        ApiError("rate_limited", retry_after=retry_after)


def test_other_errors_do_not_accept_retry_after() -> None:
    with pytest.raises(ValueError):
        ApiError("unauthenticated", retry_after=600)
