"""Callback HTTP contracts, verification boundaries, cookie isolation, and safe failures."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
import logging
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from uvicorn.logging import AccessFormatter

from app.api.cookies import AuthCookieError
from app.api.google_cookies import GOOGLE_TRANSACTION_COOKIE_NAME
from app.observability import FailureDetails, GoogleCallbackQueryFilter
from app.schemas.auth import PublicUser
from app.security.google_transactions import encode_google_transaction
from app.security.tokens import issue_refresh_token
from app.services.google_sessions import GoogleLoginResult
from app.services.login import LoginUnavailable
from google_helpers import ACCESS_TOKEN, CODE

PATH = "/api/auth/google/callback"


@pytest.fixture(params=[None, "http://127.0.0.1:5173"])
def callback(google_callback_flow, auth_settings, request):
    flow = google_callback_flow
    flow.app.state.google_settings = replace(flow.settings, frontend_origin=request.param)
    flow.destination = (request.param or "") + "/"
    session = MagicMock(spec=Session)
    result = GoogleLoginResult(
        user=PublicUser(id="101", email="fixture@example.test", created_at=datetime.now(timezone.utc)),
        refresh=issue_refresh_token(101, auth_settings),
    )

    def sessions():
        yield session

    with (
        patch("app.api.google_auth.login_session", side_effect=sessions) as opened,
        patch("app.api.google_auth.create_google_login_session", return_value=result) as saved,
        patch("app.api.google_auth.cancel_google_login") as cancel,
    ):
        flow.opened, flow.saved, flow.cancel, flow.result = opened, saved, cancel, result
        yield flow


def cookies(response):
    values = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        values.load(header)
    return values


def assert_callback_error(response, reason, destination="/", *, fallback=False):
    if fallback:
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "temporarily_unavailable"
        assert response.json()["error"]["trace_id"]
        assert "location" not in response.headers
    else:
        assert response.status_code == 302
        assert response.headers["location"] == destination + "?google_login_error=" + reason
        assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    cleared = cookies(response)
    assert set(cleared) == {GOOGLE_TRANSACTION_COOKIE_NAME}
    assert cleared[GOOGLE_TRANSACTION_COOKIE_NAME]["max-age"] == "0"
    assert cleared[GOOGLE_TRANSACTION_COOKIE_NAME]["path"] == "/api/auth"


def test_success_verifies_before_opening_db_and_returns_only_refresh_cookies(callback, auth_settings, caplog):
    original = callback.saved.side_effect

    def save(identity, session, settings):
        assert len(callback.calls) == 3
        assert identity.subject == "fixture-google-subject"
        return callback.result

    callback.saved.side_effect = save
    with caplog.at_level(logging.INFO):
        response = callback.client.get(PATH, params=callback.params)
    callback.saved.side_effect = original
    assert response.status_code == 302 and response.headers["location"] == callback.destination
    assert response.content == b""
    values = cookies(response)
    assert set(values) == {"refresh_token", "csrf_token", GOOGLE_TRANSACTION_COOKIE_NAME}
    for name in values:
        assert values[name]["path"] == "/api/auth"
        assert values[name]["httponly"] and values[name]["samesite"] == "lax"
        assert not values[name]["domain"] and not values[name]["secure"]
    assert values[GOOGLE_TRANSACTION_COOKIE_NAME]["max-age"] == "0"
    assert values["refresh_token"].value == callback.result.refresh.token
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    callback.opened.assert_called_once()
    callback.cancel.assert_not_called()
    assert "event=login_saved" in caplog.text
    for secret in (
        CODE,
        ACCESS_TOKEN,
        callback.id_token,
        callback.cookie,
        callback.transaction.state,
        callback.transaction.nonce,
        callback.transaction.code_verifier,
        callback.result.refresh.token,
    ):
        assert secret not in caplog.text and secret not in response.text
    assert auth_settings.refresh_key.hex() not in caplog.text


@pytest.mark.parametrize(
    "kind",
    [
        "no-code",
        "empty-code",
        "no-state",
        "bad-state",
        "no-cookie",
        "bad-cookie",
        "expired",
        "duplicate-code",
        "duplicate-state",
        "duplicate-error",
        "duplicate-iss",
        "wrong-iss",
        "cancelled",
        "code-and-error",
    ],
)
def test_invalid_callback_never_contacts_google_or_opens_db(callback, kind):
    params = dict(callback.params)
    if kind == "no-code":
        params.pop("code")
    elif kind == "empty-code":
        params["code"] = ""
    elif kind == "no-state":
        params.pop("state")
    elif kind == "bad-state":
        params["state"] = "fixture-private-state"
    elif kind == "no-cookie":
        callback.client.cookies.clear()
    elif kind == "bad-cookie":
        callback.client.cookies.clear()
        callback.client.cookies.set(GOOGLE_TRANSACTION_COOKIE_NAME, "fixture-private-cookie", path="/api/auth")
    elif kind == "expired":
        now = datetime.now(timezone.utc) - timedelta(seconds=301)
        old = replace(callback.transaction, issued_at=int(now.timestamp()), expires_at=int(now.timestamp()) + 300)
        callback.client.cookies.clear()
        callback.client.cookies.set(
            GOOGLE_TRANSACTION_COOKIE_NAME, encode_google_transaction(old, callback.settings, now=now), path="/api/auth"
        )
    elif kind.startswith("duplicate-"):
        name = kind.removeprefix("duplicate-")
        params = [
            *params.items(),
            (name, "fixture-private-duplicate"),
            *([(name, "other")] if name not in params else []),
        ]
    elif kind == "wrong-iss":
        params["iss"] = "https://evil.example.test"
    else:
        params["error"] = "access_denied"
        params["error_description"] = "fixture-private-error-description"
        if kind == "cancelled":
            params.pop("code")
    response = callback.client.get(PATH, params=params)
    assert_callback_error(response, "cancelled" if kind == "cancelled" else "invalid_request", callback.destination)
    assert "fixture-private" not in response.text
    assert callback.calls == []
    callback.opened.assert_not_called()
    callback.saved.assert_not_called()


@pytest.mark.parametrize("claim,value", [("nonce", "wrong"), ("email_verified", False), ("aud", "wrong")])
def test_claim_failure_does_not_open_db(callback, claim, value):
    callback.behavior["claims"] = {claim: value}
    response = callback.client.get(PATH, params=callback.params)
    assert_callback_error(response, "invalid_request", callback.destination)
    callback.opened.assert_not_called()


@pytest.mark.parametrize(
    "kind,code",
    [
        ("discovery", "temporarily_unavailable"),
        ("invalid_client", "temporarily_unavailable"),
        ("invalid_grant", "unauthenticated"),
        ("settings", "temporarily_unavailable"),
        ("auth-settings", "temporarily_unavailable"),
    ],
)
def test_provider_or_settings_failure_clears_transaction_without_db(callback, kind, code):
    if kind == "discovery":
        callback.behavior["discovery_failure"] = True
    elif kind == "settings":
        callback.app.state.google_settings = None
    elif kind == "auth-settings":
        callback.app.state.auth_settings = None
    else:
        callback.behavior["exchange_error"] = kind
    response = callback.client.get(PATH, params=callback.params)
    assert_callback_error(
        response,
        "invalid_request" if code == "unauthenticated" else code,
        callback.destination,
        fallback=kind in {"settings", "auth-settings"},
    )
    callback.opened.assert_not_called()


def test_replayed_code_with_restored_cookie_never_creates_another_session(callback):
    response = callback.client.get(PATH, params=callback.params)
    assert response.status_code == 302
    callback.client.cookies.set(GOOGLE_TRANSACTION_COOKIE_NAME, callback.cookie, path="/api/auth")
    response = callback.client.get(PATH, params=callback.params)
    assert_callback_error(response, "invalid_request", callback.destination)
    assert callback.saved.call_count == 1


def test_storage_failure_has_no_auth_cookies_and_no_compensation(callback):
    callback.saved.side_effect = LoginUnavailable(failure=FailureDetails(stage="commit", error_type="OperationalError"))
    response = callback.client.get(PATH, params=callback.params)
    assert_callback_error(response, "temporarily_unavailable", callback.destination)
    callback.cancel.assert_not_called()


@pytest.mark.parametrize("compensation_fails", [False, True])
def test_partial_cookie_failure_discards_response_and_compensates(callback, compensation_fails, caplog):
    def fail_cookie(response, refresh, settings):
        response.set_cookie("refresh_token", refresh.token)
        raise AuthCookieError("fixture-private-cookie-error")

    if compensation_fails:
        callback.cancel.side_effect = LoginUnavailable(
            failure=FailureDetails(stage="compensation", error_type="OperationalError")
        )
    with patch("app.api.google_auth.set_auth_cookies", side_effect=fail_cookie), caplog.at_level(logging.INFO):
        response = callback.client.get(PATH, params=callback.params)
    assert_callback_error(response, "temporarily_unavailable", callback.destination)
    callback.cancel.assert_called_once()
    event = "login_compensation_failed" if compensation_fails else "login_cancelled"
    assert f"event={event}" in caplog.text
    assert "fixture-private" not in caplog.text and callback.result.refresh.token not in caplog.text


def test_db_session_creation_failure_is_safe(callback):
    from app.api.errors import ApiError

    callback.opened.side_effect = ApiError(
        "temporarily_unavailable", failure=FailureDetails(stage="session", error_type="RuntimeError")
    )
    response = callback.client.get(PATH, params=callback.params)
    assert_callback_error(response, "temporarily_unavailable", callback.destination)
    callback.saved.assert_not_called()


def test_callback_schema_documents_optional_google_inputs(callback):
    operation = callback.client.get("/openapi.json").json()["paths"][PATH]["get"]
    assert {"302", "503"} <= set(operation["responses"])
    assert "401" not in operation["responses"]
    assert "google_login_error" in operation["responses"]["302"]["description"]
    assert {item["name"] for item in operation["parameters"]} == {"code", "state", "error", "iss"}
    assert all(not item["required"] for item in operation["parameters"])


def test_access_log_filter_preserves_uvicorn_formatter_and_removes_entire_query():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", PATH + "?code=fixture-secret&state=fixture-state", "1.1", 401),
        None,
    )
    GoogleCallbackQueryFilter().filter(record)
    formatted = AccessFormatter("%(request_line)s %(status_code)s", use_colors=False).format(record)
    assert formatted.startswith("GET " + PATH + " HTTP/1.1")
    assert "fixture" not in formatted and "?" not in formatted


def test_callback_ignores_untrusted_return_to_query(callback):
    response = callback.client.get(PATH, params={**callback.params, "return_to": "https://evil.example.test"})
    assert response.status_code == 302 and response.headers["location"] == callback.destination


def test_failure_keeps_preexisting_refresh_and_csrf_cookies(callback):
    callback.client.cookies.set("refresh_token", "fixture-existing-refresh", path="/api/auth")
    callback.client.cookies.set("csrf_token", "fixture-existing-csrf", path="/api/auth")
    response = callback.client.get(PATH, params={**callback.params, "state": "wrong"})
    assert_callback_error(response, "invalid_request", callback.destination)
    assert callback.client.cookies.get("refresh_token") == "fixture-existing-refresh"
    assert callback.client.cookies.get("csrf_token") == "fixture-existing-csrf"


def test_secure_cookie_policy_applies_to_success_and_error_cleanup(callback, auth_settings):
    callback.app.state.auth_settings = replace(auth_settings, cookie_secure=True)
    response = callback.client.get(PATH, params=callback.params)
    assert response.status_code == 302
    assert all(item["secure"] for item in cookies(response).values())
    response = callback.client.get(PATH, params={"state": "wrong"})
    assert_callback_error(response, "invalid_request", callback.destination)
    assert cookies(response)[GOOGLE_TRANSACTION_COOKIE_NAME]["secure"]


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example.test"},
        {"Origin": "https://evil.example.test"},
        {"Forwarded": 'host="evil.example.test";proto=https'},
        {"X-Forwarded-Host": "evil.example.test", "X-Forwarded-Proto": "https"},
    ],
)
def test_frontend_destination_ignores_request_headers_and_extra_queries(callback, headers):
    response = callback.client.get(
        PATH,
        params={**callback.params, "return_to": "//evil.example.test", "frontend_origin": "https://evil.test"},
        headers=headers,
    )
    assert response.status_code == 302
    assert response.headers["location"] == callback.destination
    assert "?" not in response.headers["location"] and "#" not in response.headers["location"]
    assert callback.result.refresh.token not in response.headers["location"]
    assert CODE not in response.headers["location"]


@pytest.mark.parametrize(
    "provider_error,expected",
    [
        ("access_denied", "cancelled"),
        ("server_error", "temporarily_unavailable"),
        ("temporarily_unavailable", "temporarily_unavailable"),
        ("fixture-private-error", "invalid_request"),
        ("", "invalid_request"),
    ],
)
@pytest.mark.parametrize("valid_state", [True, False])
def test_provider_error_is_classified_only_after_transaction_validation(
    callback, provider_error, expected, valid_state, caplog
):
    params = {
        "state": callback.transaction.state if valid_state else "fixture-private-state",
        "error": provider_error,
        "error_description": "fixture-private-description",
        "return_to": "//evil.example.test",
    }
    with caplog.at_level(logging.INFO):
        response = callback.client.get(PATH, params=params)
    reason = expected if valid_state else "invalid_request"
    assert_callback_error(response, reason, callback.destination)
    assert f"reason={reason}" in caplog.text
    assert "fixture-private" not in caplog.text + str(response.headers) + response.text
    assert callback.calls == []
    callback.opened.assert_not_called()
    callback.saved.assert_not_called()
    if valid_state and reason != "invalid_request":
        assert "event=google_callback_verification_failed" not in caplog.text


@pytest.mark.parametrize("broken", ["RedirectResponse", "clear_google_transaction_cookie"])
def test_failure_response_preparation_has_bounded_json_fallback(callback, broken, caplog):
    with (
        patch("app.api.google_auth." + broken, side_effect=RuntimeError("fixture-private-response")) as failed,
        caplog.at_level(logging.INFO),
    ):
        response = callback.client.get(PATH, params={"state": "wrong"})
    assert_callback_error(response, "temporarily_unavailable", fallback=True)
    failed.assert_called_once()
    assert "stage=cookie error_type=RuntimeError" in caplog.text
    assert "fixture-private" not in caplog.text + response.text
    callback.saved.assert_not_called()


def test_committed_session_is_compensated_when_success_redirect_cannot_be_built(callback):
    with patch("app.api.google_auth.RedirectResponse", side_effect=RuntimeError("fixture-private-response")):
        response = callback.client.get(PATH, params=callback.params)
    assert_callback_error(response, "temporarily_unavailable", fallback=True)
    callback.cancel.assert_called_once()


def test_repeated_transaction_cleanup_failure_discards_auth_cookies_and_compensates(callback):
    with patch(
        "app.api.google_auth.clear_google_transaction_cookie", side_effect=RuntimeError("fixture-private-cleanup")
    ):
        response = callback.client.get(PATH, params=callback.params)
    assert_callback_error(response, "temporarily_unavailable", fallback=True)
    callback.cancel.assert_called_once()


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example.test"},
        {"Origin": "https://evil.example.test", "Referer": "https://evil.example.test"},
        {"Forwarded": 'host="evil.example.test";proto=https'},
        {"X-Forwarded-Host": "evil.example.test", "X-Forwarded-Proto": "https"},
    ],
)
def test_failure_destination_ignores_headers_and_unverified_return_to(callback, headers):
    response = callback.client.get(
        PATH,
        params={"state": "wrong", "return_to": "//evil.example.test", "frontend_origin": "https://evil.test"},
        headers=headers,
    )
    assert_callback_error(response, "invalid_request", callback.destination)
    callback.opened.assert_not_called()
