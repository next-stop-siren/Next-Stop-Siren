"""HTTP contracts and pre-DB guards for real cookie session routes, with mocked services."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError
from app.api.errors import ApiError
from app.main import create_app
from app.observability import FailureDetails
from app.security.session_values import create_csrf_token
from app.security.tokens import issue_access_token, issue_refresh_token
from cookie_session_helpers import seed_auth_cookies

from app.services.refresh import (
    InvalidRefresh,
    RefreshLimitReached,
    RefreshRace,
    RefreshResult,
    RefreshReuseDetected,
    RefreshUnavailable,
)

MODULE = "app.api.cookie_sessions"
ORIGIN = "http://127.0.0.1:5173"
PATHS = ["/api/auth/csrf", "/api/auth/refresh", "/api/auth/logout"]


@pytest.fixture
def api(auth_settings):
    original = issue_refresh_token(101, auth_settings, now=datetime.now(timezone.utc) - timedelta(days=7))
    result = RefreshResult(
        access=issue_access_token(101, auth_settings),
        refresh=issue_refresh_token(101, auth_settings, absolute_expires_at=original.expires_at),
    )
    session = Mock(spec=Session)
    opened = Mock()
    csrf = create_csrf_token(original.jti, auth_settings)

    def sessions():
        opened()
        yield session

    with (
        patch(f"{MODULE}.registration_session", side_effect=sessions),
        patch(f"{MODULE}.read_csrf_token", return_value=csrf) as read,
        patch(f"{MODULE}.rotate_refresh_session", return_value=result) as rotate,
        patch(f"{MODULE}.logout_refresh_session") as logout,
        patch(f"{MODULE}.cancel_refresh_rotation") as cancel,
        TestClient(create_app(auth_settings), base_url="http://127.0.0.1:8000") as client,
    ):
        seed_auth_cookies(client, original, auth_settings)
        yield SimpleNamespace(
            client=client,
            original=original,
            result=result,
            session=session,
            opened=opened,
            read=read,
            rotate=rotate,
            logout=logout,
            cancel=cancel,
            settings=auth_settings,
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )


def error(response, status, code, *, cookies=False):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code and body["error"]["fields"] == {}
    assert body["error"]["trace_id"]
    assert "private" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    if not cookies:
        assert "set-cookie" not in response.headers
    else:
        headers = response.headers.get_list("set-cookie")
        assert len(headers) == 2 and all("Max-Age=0" in item and "Path=/api/auth" in item for item in headers)
    return body["error"]


def test_csrf_get_has_no_origin_requirement_or_cookie_changes(api):
    api.client.cookies.delete("csrf_token", domain="127.0.0.1", path="/api/auth")
    response = api.client.get(PATHS[0])
    assert response.status_code == 200
    assert response.json() == {"csrf_token": api.headers["X-CSRF-Token"]}
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    api.read.assert_called_once_with(api.original.token, api.session, api.settings)
    api.rotate.assert_not_called()


def test_refresh_success_has_exact_body_and_host_only_cookies(api):
    response = api.client.post(PATHS[1], headers=api.headers)
    assert response.status_code == 200
    assert response.json() == {
        "access_token": api.result.access.token,
        "token_type": "Bearer",
        "expires_in": 900,
        "csrf_token": create_csrf_token(api.result.refresh.jti, api.settings),
    }
    assert response.cookies["refresh_token"] == api.result.refresh.token
    assert api.result.refresh.token not in response.text
    assert len(response.headers.get_list("set-cookie")) == 2
    assert all(
        "HttpOnly" in item and "SameSite=lax" in item and "Path=/api/auth" in item and "Domain=" not in item
        for item in response.headers.get_list("set-cookie")
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    api.rotate.assert_called_once_with(api.original.token, api.session, api.settings)
    api.cancel.assert_not_called()


def test_logout_success_is_empty_204_with_two_expired_cookies(api):
    response = api.client.post(PATHS[2], headers=api.headers)
    assert response.status_code == 204 and response.content == b""
    assert "content-type" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers.get_list("set-cookie")) == 2
    assert all("Max-Age=0" in item for item in response.headers.get_list("set-cookie"))
    assert not api.client.cookies
    api.logout.assert_called_once_with(api.original.token, api.session, api.settings)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("kind", ["missing", "malformed", "access", "expired"])
def test_bad_refresh_fails_before_database(api, path, kind):
    api.client.cookies.delete("refresh_token", domain="127.0.0.1", path="/api/auth")
    if kind != "missing":
        token = {
            "malformed": "private-invalid-jwt",
            "access": issue_access_token(101, api.settings).token,
            "expired": issue_refresh_token(
                101, api.settings, now=datetime.now(timezone.utc) - timedelta(days=15)
            ).token,
        }[kind]
        api.client.cookies.set("refresh_token", token, domain="127.0.0.1", path="/api/auth")
    response = api.client.get(path) if path == PATHS[0] else api.client.post(path, headers=api.headers)
    error(response, 401, "unauthenticated")
    api.opened.assert_not_called()
    api.rotate.assert_not_called()
    api.logout.assert_not_called()
    api.read.assert_not_called()


@pytest.mark.parametrize("path", PATHS[1:])
@pytest.mark.parametrize("failure", ["origin-missing", "origin-wrong", "csrf-missing", "csrf-wrong", "csrf-duplicate"])
def test_post_guard_failure_never_opens_database(api, path, failure):
    headers = dict(api.headers)
    if failure == "origin-missing":
        del headers["Origin"]
    elif failure == "origin-wrong":
        headers["Origin"] = "http://evil.example"
    elif failure == "csrf-missing":
        del headers["X-CSRF-Token"]
    elif failure == "csrf-wrong":
        headers["X-CSRF-Token"] = "wrong"
    else:
        headers = list(headers.items()) + [("X-CSRF-Token", headers["X-CSRF-Token"])]
    error(api.client.post(path, headers=headers), 403, "csrf_failed")
    api.opened.assert_not_called()
    api.rotate.assert_not_called()
    api.logout.assert_not_called()


def test_race_is_409_retry_after_one_and_never_changes_cookies(api):
    api.rotate.side_effect = RefreshRace()
    before = dict(api.client.cookies)
    response = api.client.post(PATHS[1], headers=api.headers)
    error(response, 409, "refresh_race")
    assert response.headers["retry-after"] == "1"
    assert response.headers["access-control-expose-headers"] == "Retry-After"
    assert dict(api.client.cookies) == before
    api.cancel.assert_not_called()


@pytest.mark.parametrize("failure", [RefreshReuseDetected, RefreshLimitReached])
def test_committed_revocation_is_401_and_expires_cookies(api, failure):
    api.rotate.side_effect = failure()
    error(api.client.post(PATHS[1], headers=api.headers), 401, "unauthenticated", cookies=True)
    assert not api.client.cookies
    api.cancel.assert_not_called()


@pytest.mark.parametrize("operation,path", [("read", PATHS[0]), ("rotate", PATHS[1]), ("logout", PATHS[2])])
@pytest.mark.parametrize("failure", ["invalid", "unavailable"])
def test_service_failure_contract(api, operation, path, failure, caplog):
    getattr(api, operation).side_effect = (
        InvalidRefresh()
        if failure == "invalid"
        else RefreshUnavailable(failure=FailureDetails(stage="commit", error_type="OperationalError"))
    )
    response = api.client.get(path) if operation == "read" else api.client.post(path, headers=api.headers)
    detail = error(
        response,
        401 if failure == "invalid" else 503,
        "unauthenticated" if failure == "invalid" else "temporarily_unavailable",
    )
    if failure == "unavailable":
        assert detail["trace_id"] in caplog.text and "stage=commit" in caplog.text
    assert api.original.token not in caplog.text


@pytest.mark.parametrize("failure", ["cookie", "body"])
def test_response_preparation_failure_compensates_and_discards_partial_cookies(api, failure):
    def partial(response, refresh, settings):
        response.set_cookie("refresh_token", refresh.token)
        raise AuthCookieError("private cookie details")

    target = (
        patch(f"{MODULE}.set_auth_cookies", side_effect=partial)
        if failure == "cookie"
        else patch(f"{MODULE}.RefreshResponse", side_effect=ValueError("private response details"))
    )
    with target:
        response = api.client.post(PATHS[1], headers=api.headers)
    error(response, 503, "temporarily_unavailable")
    assert api.result.access.token not in response.text and api.result.refresh.token not in response.text
    api.cancel.assert_called_once_with(101, api.result.refresh, api.session, api.settings)


def test_compensation_failure_is_logged_without_returning_credentials(api, caplog):
    api.cancel.side_effect = RefreshUnavailable(
        failure=FailureDetails(stage="compensation", error_type="OperationalError")
    )
    with patch(f"{MODULE}.set_auth_cookies", side_effect=AuthCookieError("private cookie")):
        response = api.client.post(PATHS[1], headers=api.headers)
    detail = error(response, 503, "temporarily_unavailable")
    assert "event=refresh_compensation_failed" in caplog.text and detail["trace_id"] in caplog.text
    assert api.result.refresh.token not in caplog.text and "private" not in caplog.text


def test_logout_cookie_failure_never_returns_partial_204(api):
    def partial(response, settings):
        response.delete_cookie("refresh_token")
        raise AuthCookieError("private cookie")

    with patch(f"{MODULE}.clear_auth_cookies", side_effect=partial):
        error(api.client.post(PATHS[2], headers=api.headers), 503, "temporarily_unavailable")
    api.logout.assert_called_once()


def test_openapi_documents_cookie_routes_without_bearer_or_request_body(api):
    paths = api.client.get("/openapi.json").json()["paths"]
    for path in PATHS:
        operation = paths[path]["get" if path == PATHS[0] else "post"]
        assert "requestBody" not in operation and not operation.get("security")
        expected = (
            {"200", "401", "503"}
            if path == PATHS[0]
            else ({"200", "401", "403", "409", "503"} if path == PATHS[1] else {"204", "401", "403", "503"})
        )
        assert expected <= set(operation["responses"])
        if path != PATHS[0]:
            csrf_header = next(item for item in operation["parameters"] if item["name"] == "X-CSRF-Token")
            assert csrf_header["in"] == "header" and csrf_header["required"] is False
    assert "content" not in paths[PATHS[2]]["post"]["responses"]["204"]


@pytest.mark.parametrize("delay", [None, True, 0, 2])
def test_race_api_error_requires_exact_one_second(delay):
    with pytest.raises(ValueError):
        ApiError("refresh_race", retry_after=delay)


def test_cookie_clear_flag_cannot_be_used_for_race_or_service_failure():
    with pytest.raises(ValueError):
        ApiError("refresh_race", retry_after=1, clear_cookies=True)
    with pytest.raises(ValueError):
        ApiError("temporarily_unavailable", clear_cookies=True)


def test_cookie_expiration_failure_after_revocation_discards_partial_headers(api, caplog):
    api.rotate.side_effect = RefreshReuseDetected()

    def partial(response, settings):
        response.delete_cookie("refresh_token")
        raise AuthCookieError("private cookie details")

    with patch("app.api.errors.clear_auth_cookies", side_effect=partial):
        response = api.client.post(PATHS[1], headers=api.headers)
    detail = error(response, 503, "temporarily_unavailable")
    assert "event=authentication_cookie_failed" in caplog.text and detail["trace_id"] in caplog.text
    assert "private" not in caplog.text
    assert "www-authenticate" not in response.headers
