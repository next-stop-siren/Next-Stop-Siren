"""Inspect real Google Set-Cookie headers without a Google account or database."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie

from fastapi import Response
import pytest

from app.api.google_cookies import (
    GOOGLE_TRANSACTION_COOKIE_NAME,
    clear_google_transaction_cookie,
    set_google_transaction_cookie,
)
from app.security.google_transactions import (
    GoogleTransactionError,
    create_google_transaction,
    verify_google_transaction,
)
from app.security.settings import AuthSettings, GoogleSettings

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def cookie_from(response):
    headers = [value.decode("latin-1") for name, value in response.raw_headers if name == b"set-cookie"]
    assert len(headers) == 1
    cookies = SimpleCookie()
    cookies.load(headers[0])
    assert set(cookies) == {GOOGLE_TRANSACTION_COOKIE_NAME}
    return cookies[GOOGLE_TRANSACTION_COOKIE_NAME]


@pytest.mark.parametrize("production", [False, True])
def test_encrypted_cookie_attributes_and_clear_match_policy(google_environment, production):
    if production:
        google_environment.update(
            {
                "AUTH_ENV": "production",
                "AUTH_API_ORIGIN": "https://api.example.test",
                "AUTH_ALLOWED_ORIGINS": "https://app.example.test",
                "AUTH_COOKIE_SECURE": "true",
                "GOOGLE_REDIRECT_URI": "https://api.example.test/api/auth/google/callback",
            }
        )
    auth = AuthSettings.from_env(google_environment)
    google = GoogleSettings.from_env(google_environment, auth_settings=auth)
    transaction = create_google_transaction(now=NOW)
    response = Response()
    set_google_transaction_cookie(response, transaction, google, auth, now=NOW)
    cookie = cookie_from(response)
    assert cookie["path"] == "/api/auth"
    assert cookie["domain"] == ""
    assert cookie["httponly"] is True
    assert bool(cookie["secure"]) is production
    assert cookie["samesite"] == "lax"
    assert cookie["max-age"] == "300"
    assert parsedate_to_datetime(cookie["expires"]) == NOW + timedelta(seconds=300)
    assert verify_google_transaction(cookie.value, transaction.state, google, now=NOW) == transaction
    cleared = Response()
    before_clear = datetime.now(timezone.utc).replace(microsecond=0)
    clear_google_transaction_cookie(cleared, auth)
    after_clear = datetime.now(timezone.utc)
    expired = cookie_from(cleared)
    assert expired.value == ""
    assert expired["max-age"] == "0"
    assert before_clear <= parsedate_to_datetime(expired["expires"]) <= after_clear
    for attribute in ("path", "domain", "httponly", "secure", "samesite"):
        assert expired[attribute] == cookie[attribute]


def test_delayed_cookie_issuance_does_not_extend_absolute_expiry(auth_settings, google_settings):
    transaction = create_google_transaction(now=NOW)
    response = Response()
    set_google_transaction_cookie(
        response, transaction, google_settings, auth_settings, now=NOW + timedelta(seconds=10.5)
    )
    cookie = cookie_from(response)
    assert cookie["max-age"] == "289"
    assert parsedate_to_datetime(cookie["expires"]) == NOW + timedelta(seconds=300)


@pytest.mark.parametrize("seconds", [-1, 299.5, 300, 301])
def test_invalid_time_produces_no_cookie(auth_settings, google_settings, seconds):
    transaction = create_google_transaction(now=NOW)
    response = Response()
    with pytest.raises(GoogleTransactionError):
        set_google_transaction_cookie(
            response, transaction, google_settings, auth_settings, now=NOW + timedelta(seconds=seconds)
        )
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("field,value", [("state", "short"), ("return_to", "//evil.test"), ("expires_at", 9999999999)])
def test_invalid_transaction_never_sets_headers(auth_settings, google_settings, field, value):
    transaction = replace(create_google_transaction(now=NOW), **{field: value})
    response = Response()
    with pytest.raises(GoogleTransactionError):
        set_google_transaction_cookie(response, transaction, google_settings, auth_settings, now=NOW)
    assert "set-cookie" not in response.headers


def test_naive_time_produces_no_cookie(auth_settings, google_settings):
    transaction = create_google_transaction(now=NOW)
    response = Response()
    with pytest.raises(GoogleTransactionError):
        set_google_transaction_cookie(
            response, transaction, google_settings, auth_settings, now=NOW.replace(tzinfo=None)
        )
    assert "set-cookie" not in response.headers


def test_default_clock_can_issue_cookie(auth_settings, google_settings):
    transaction = create_google_transaction()
    response = Response()
    set_google_transaction_cookie(response, transaction, google_settings, auth_settings)
    assert 0 < int(cookie_from(response)["max-age"]) <= 300


def test_clearing_transaction_cookie_preserves_existing_auth_cookies(auth_settings):
    response = Response()
    response.set_cookie("refresh_token", "fixture-refresh")
    response.set_cookie("csrf_token", "fixture-csrf")
    original_headers = list(response.raw_headers)
    clear_google_transaction_cookie(response, auth_settings)
    assert response.raw_headers[:-1] == original_headers
    cookies = SimpleCookie()
    cookies.load(response.raw_headers[-1][1].decode("latin-1"))
    assert set(cookies) == {GOOGLE_TRANSACTION_COOKIE_NAME}
    assert cookies[GOOGLE_TRANSACTION_COOKIE_NAME]["max-age"] == "0"
