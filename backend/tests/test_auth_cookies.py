"""Inspect the real Set-Cookie headers without a browser or database."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie

from fastapi import Response
import pytest

from app.api.cookies import AuthCookieError, CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME, clear_auth_cookies, set_auth_cookies
from app.security.session_values import create_csrf_token
from app.security.settings import AuthSettings
from app.security.tokens import issue_access_token, issue_refresh_token


def response_cookies(response: Response) -> SimpleCookie:
    result: SimpleCookie = SimpleCookie()
    headers = [value for name, value in response.raw_headers if name == b"set-cookie"]
    assert len(headers) == 2
    for value in headers:
        result.load(value.decode("latin-1"))
    return result


@pytest.mark.parametrize("production", [False, True])
def test_cookie_attributes_and_values_follow_policy(auth_environment, production: bool) -> None:
    if production:
        auth_environment.update(
            {
                "AUTH_ENV": "production",
                "AUTH_API_ORIGIN": "https://api.example.test",
                "AUTH_ALLOWED_ORIGINS": "https://app.example.test",
                "AUTH_COOKIE_SECURE": "true",
            }
        )
    settings = AuthSettings.from_env(auth_environment)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    refresh = issue_refresh_token(101, settings, now=now)
    response = Response()
    csrf = set_auth_cookies(response, refresh, settings, now=now)
    cookies = response_cookies(response)
    assert set(cookies) == {REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME}
    assert cookies[REFRESH_COOKIE_NAME].value == refresh.token
    assert cookies[CSRF_COOKIE_NAME].value == csrf == create_csrf_token(refresh.jti, settings)
    for cookie in cookies.values():
        assert cookie["httponly"] is True
        assert bool(cookie["secure"]) is production
        assert cookie["samesite"] == "lax"
        assert cookie["path"] == "/api/auth"
        assert cookie["domain"] == ""
        assert cookie["max-age"] == "1209600"
        assert parsedate_to_datetime(cookie["expires"]) == refresh.expires_at


def test_cookie_max_age_uses_remaining_lifetime_without_extending_expiry(auth_settings: AuthSettings) -> None:
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    refresh = issue_refresh_token(101, auth_settings, now=issued_at)
    now = issued_at + timedelta(seconds=10, microseconds=500000)
    response = Response()
    set_auth_cookies(response, refresh, auth_settings, now=now)
    for cookie in response_cookies(response).values():
        assert cookie["max-age"] == "1209589"
        assert parsedate_to_datetime(cookie["expires"]) == refresh.expires_at


def test_default_clock_and_non_utc_metadata_are_supported(auth_settings: AuthSettings) -> None:
    refresh = issue_refresh_token(101, auth_settings)
    refresh = replace(refresh, expires_at=refresh.expires_at.astimezone(timezone(timedelta(hours=9))))
    response = Response()
    before = datetime.now(timezone.utc)
    set_auth_cookies(response, refresh, auth_settings)
    after = datetime.now(timezone.utc)
    for cookie in response_cookies(response).values():
        assert int((refresh.expires_at - after).total_seconds()) <= int(cookie["max-age"])
        assert int(cookie["max-age"]) <= int((refresh.expires_at - before).total_seconds())
        assert parsedate_to_datetime(cookie["expires"]) == refresh.expires_at


@pytest.mark.parametrize("seconds_remaining", [-1, 0, 0.5])
def test_expired_or_subsecond_lifetime_produces_no_cookies(
    auth_settings: AuthSettings, seconds_remaining: float
) -> None:
    refresh = issue_refresh_token(101, auth_settings)
    now = refresh.expires_at - timedelta(seconds=seconds_remaining)
    response = Response()
    with pytest.raises(AuthCookieError):
        set_auth_cookies(response, refresh, auth_settings, now=now)
    assert "set-cookie" not in response.headers


def test_access_token_cannot_be_put_in_cookies(auth_settings: AuthSettings) -> None:
    response = Response()
    with pytest.raises(AuthCookieError):
        set_auth_cookies(response, issue_access_token(101, auth_settings), auth_settings)
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("naive_field", ["now", "expires_at"])
def test_naive_timestamp_produces_no_cookies(auth_settings: AuthSettings, naive_field: str) -> None:
    refresh = issue_refresh_token(101, auth_settings)
    now = refresh.issued_at
    if naive_field == "now":
        now = now.replace(tzinfo=None)
    else:
        refresh = replace(refresh, expires_at=refresh.expires_at.replace(tzinfo=None))
    response = Response()
    with pytest.raises(AuthCookieError):
        set_auth_cookies(response, refresh, auth_settings, now=now)
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("production", [False, True])
def test_cookie_expiration_matches_issuance_attributes(auth_settings, production):
    settings = replace(auth_settings, cookie_secure=production)
    issued = Response()
    set_auth_cookies(issued, issue_refresh_token(101, settings), settings)
    cleared = Response(status_code=204)
    clear_auth_cookies(cleared, settings)
    before, after = response_cookies(issued), response_cookies(cleared)
    assert set(after) == set(before) == {REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME}
    for name, cookie in after.items():
        assert cookie.value == ""
        assert cookie["max-age"] == "0"
        assert parsedate_to_datetime(cookie["expires"]) <= datetime.now(timezone.utc)
        for attribute in ("path", "domain", "secure", "httponly", "samesite"):
            assert cookie[attribute] == before[name][attribute]
    assert cleared.status_code == 204 and cleared.body == b""
