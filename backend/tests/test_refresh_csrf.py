"""Exercise cookie authentication dependencies on a test-only route, without DB access."""

from datetime import datetime, timedelta, timezone
from typing import Annotated
from unittest.mock import Mock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.auth import require_refresh_csrf
from app.api.errors import register_error_handlers
from app.security.session_values import create_csrf_token
from app.security.tokens import VerifiedToken, issue_access_token, issue_refresh_token

ORIGIN = "http://127.0.0.1:5173"
PATH = "/api/auth/phase2-probe"


@pytest.fixture
def protected(auth_settings):
    app = FastAPI()
    app.state.auth_settings = auth_settings
    register_error_handlers(app)
    changed = Mock()

    @app.post(PATH)
    def probe(refresh: Annotated[VerifiedToken, Depends(require_refresh_csrf)]):
        changed()
        return {"user_id": str(refresh.user_id)}

    refresh = issue_refresh_token(101, auth_settings)
    csrf = create_csrf_token(refresh.jti, auth_settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.cookies.set("refresh_token", refresh.token, path="/api/auth")
        client.cookies.set("csrf_token", csrf, path="/api/auth")
        yield client, changed, refresh, csrf


def assert_rejected(protected, headers, status=403):
    client, changed, refresh, csrf = protected
    before = dict(client.cookies)
    response = client.post(PATH, headers=headers)
    assert response.status_code == status
    assert response.json()["error"]["code"] == ("csrf_failed" if status == 403 else "unauthenticated")
    assert "set-cookie" not in response.headers
    assert dict(client.cookies) == before
    assert refresh.token not in response.text and csrf not in response.text
    changed.assert_not_called()


def test_matching_origin_cookie_header_and_refresh_are_accepted(protected):
    client, changed, _, csrf = protected
    response = client.post(PATH, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json() == {"user_id": "101"}
    assert "set-cookie" not in response.headers
    changed.assert_called_once()


@pytest.mark.parametrize("origin", [None, "null", "http://evil.example", "http://localhost:5173", ORIGIN + "/"])
def test_missing_or_inexact_origin_is_rejected(protected, origin):
    headers = {"X-CSRF-Token": protected[3]}
    if origin is not None:
        headers["Origin"] = origin
    assert_rejected(protected, headers)


def test_duplicate_origin_is_rejected(protected):
    assert_rejected(protected, [("Origin", ORIGIN), ("Origin", ORIGIN), ("X-CSRF-Token", protected[3])])


@pytest.mark.parametrize("header", [None, "", "wrong", "x" * 43, " " * 43, "x" * 10000])
def test_missing_or_wrong_csrf_header_is_rejected(protected, header):
    headers = {"Origin": ORIGIN}
    if header is not None:
        headers["X-CSRF-Token"] = header
    assert_rejected(protected, headers)


def test_duplicate_csrf_header_is_rejected(protected):
    csrf = protected[3]
    assert_rejected(protected, [("Origin", ORIGIN), ("X-CSRF-Token", csrf), ("X-CSRF-Token", csrf)])


def test_missing_csrf_cookie_is_rejected(protected):
    client, _, _, csrf = protected
    client.cookies.delete("csrf_token", path="/api/auth")
    assert_rejected(protected, {"Origin": ORIGIN, "X-CSRF-Token": csrf})


def test_matching_cookie_and_header_must_still_be_bound_to_current_refresh(protected, auth_settings):
    client, _, _, _ = protected
    other = issue_refresh_token(101, auth_settings)
    stale = create_csrf_token(other.jti, auth_settings)
    client.cookies.set("csrf_token", stale, path="/api/auth")
    assert_rejected(protected, {"Origin": ORIGIN, "X-CSRF-Token": stale})


def test_stale_header_with_new_cookies_is_403_and_current_pair_can_retry(protected, auth_settings):
    client, changed, _, stale = protected
    new = issue_refresh_token(101, auth_settings)
    csrf = create_csrf_token(new.jti, auth_settings)
    client.cookies.set("refresh_token", new.token, path="/api/auth")
    client.cookies.set("csrf_token", csrf, path="/api/auth")
    assert_rejected(protected, {"Origin": ORIGIN, "X-CSRF-Token": stale})
    assert client.post(PATH, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 200
    changed.assert_called_once()


@pytest.mark.parametrize("kind", ["missing", "malformed", "access", "expired"])
def test_invalid_refresh_is_401_before_change(protected, auth_settings, kind):
    client, _, _, csrf = protected
    client.cookies.delete("refresh_token", path="/api/auth")
    if kind != "missing":
        token = {
            "malformed": "private-invalid-jwt",
            "access": issue_access_token(101, auth_settings).token,
            "expired": issue_refresh_token(
                101, auth_settings, now=datetime.now(timezone.utc) - timedelta(days=15)
            ).token,
        }[kind]
        client.cookies.set("refresh_token", token, path="/api/auth")
    assert_rejected(protected, {"Origin": ORIGIN, "X-CSRF-Token": csrf}, status=401)
