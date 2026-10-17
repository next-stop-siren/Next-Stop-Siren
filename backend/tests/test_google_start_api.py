"""Google start HTTP contracts using real Authlib and mocked discovery HTTP."""

import asyncio
from dataclasses import replace
from http.cookies import SimpleCookie
import logging
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest

from app.api.google_auth import google_http_client
from app.api.google_cookies import GOOGLE_TRANSACTION_COOKIE_NAME
from app.integrations.google_oidc import GOOGLE_AUTHORIZATION_ENDPOINT, GOOGLE_ISSUER
from app.main import create_app
from app.security.google_transactions import GoogleTransactionError, verify_google_transaction
from app.security.settings import AuthSettings, GoogleSettings

PATH = "/api/auth/google/start"
METADATA = {"issuer": GOOGLE_ISSUER, "authorization_endpoint": GOOGLE_AUTHORIZATION_ENDPOINT}


@pytest.fixture
def google_start_client(auth_settings, google_settings):
    calls = []
    behavior = {"status": 200, "metadata": dict(METADATA)}

    async def discovery(request):
        calls.append(request)
        if "exception" in behavior:
            raise behavior["exception"]
        if "delay" in behavior:
            await asyncio.sleep(behavior["delay"])
        if "content" in behavior:
            return httpx.Response(behavior["status"], content=behavior["content"])
        return httpx.Response(behavior["status"], json=behavior["metadata"])

    async def mock_http_client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(discovery), trust_env=False) as client:
            yield client

    app = create_app(auth_settings, google_settings)
    app.dependency_overrides[google_http_client] = mock_http_client
    with (
        patch("app.api.auth.get_session") as sessions,
        TestClient(app, base_url=auth_settings.api_origin, follow_redirects=False) as client,
    ):
        yield client, calls, behavior, sessions, app


def assert_error(response, status, code):
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["trace_id"]
    assert "location" not in response.headers
    assert "set-cookie" not in response.headers
    return error


def transaction_from(response, google_settings):
    location = urlsplit(response.headers["location"])
    assert location.scheme == "https"
    assert location.netloc == "accounts.google.com"
    assert location.path == "/o/oauth2/v2/auth"
    query = parse_qs(location.query)
    headers = response.headers.get_list("set-cookie")
    assert len(headers) == 1
    cookies = SimpleCookie()
    cookies.load(headers[0])
    assert set(cookies) == {GOOGLE_TRANSACTION_COOKIE_NAME}
    transaction = verify_google_transaction(
        cookies[GOOGLE_TRANSACTION_COOKIE_NAME].value, query["state"][0], google_settings
    )
    return transaction, query, cookies[GOOGLE_TRANSACTION_COOKIE_NAME]


@pytest.mark.parametrize("return_to", [None, "/"])
@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:5173"])
def test_start_returns_302_and_matching_encrypted_transaction(google_start_client, google_settings, return_to, origin):
    client, calls, _, sessions, _ = google_start_client
    response = client.get(
        PATH,
        params={} if return_to is None else {"return_to": return_to},
        headers={} if origin is None else {"Origin": origin},
    )
    assert response.status_code == 302
    transaction, query, cookie = transaction_from(response, google_settings)
    assert query == {
        "client_id": [google_settings.client_id],
        "response_type": ["code"],
        "redirect_uri": [google_settings.redirect_uri],
        "scope": ["openid email"],
        "state": [transaction.state],
        "nonce": [transaction.nonce],
        "code_challenge": [transaction.code_challenge],
        "code_challenge_method": ["S256"],
    }
    assert transaction.return_to == "/"
    assert transaction.code_verifier not in response.headers["location"]
    assert google_settings.client_secret not in str(response.headers)
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert cookie["path"] == "/api/auth" and cookie["domain"] == ""
    assert cookie["httponly"] is True and cookie["samesite"] == "lax"
    assert 0 < int(cookie["max-age"]) <= 300
    assert len(calls) == 1
    assert str(calls[0].url) == google_settings.discovery_url
    assert calls[0].method == "GET" and calls[0].content == b""
    assert "authorization" not in calls[0].headers
    assert google_settings.client_secret not in str(calls[0].headers)
    sessions.assert_not_called()


@pytest.mark.parametrize(
    "return_to",
    [
        "",
        "//evil.example.test",
        "https://evil.example.test",
        "/chat",
        "/?next=evil",
        "/#evil",
        "/\\evil",
        "/%2fevil",
        "/\n",
    ],
)
def test_invalid_return_to_fails_before_google_or_database(google_start_client, return_to):
    client, calls, _, sessions, _ = google_start_client
    response = client.get(PATH, params={"return_to": return_to})
    error = assert_error(response, 422, "validation_error")
    assert set(error["fields"]) == {"return_to"}
    assert calls == []
    sessions.assert_not_called()


def test_duplicate_return_to_is_rejected(google_start_client):
    client, calls, _, sessions, _ = google_start_client
    assert_error(client.get(PATH, params=[("return_to", "/"), ("return_to", "/")]), 422, "validation_error")
    assert calls == []
    sessions.assert_not_called()


def test_query_cannot_override_server_oauth_parameters(google_start_client, google_settings):
    client, _, _, _, _ = google_start_client
    response = client.get(
        PATH,
        params={
            "client_id": "evil",
            "redirect_uri": "https://evil.test",
            "state": "evil",
            "nonce": "evil",
            "code_challenge_method": "plain",
            "scope": "extra-permission",
        },
    )
    transaction, query, _ = transaction_from(response, google_settings)
    assert query["client_id"] == [google_settings.client_id]
    assert query["redirect_uri"] == [google_settings.redirect_uri]
    assert query["state"] == [transaction.state] and query["nonce"] == [transaction.nonce]
    assert query["scope"] == ["openid email"] and query["code_challenge_method"] == ["S256"]


def test_new_start_replaces_transaction_and_rejects_previous_state(google_start_client, google_settings):
    client, _, _, sessions, _ = google_start_client
    first = client.get(PATH)
    second = client.get(PATH)
    old, _, _ = transaction_from(first, google_settings)
    new, _, cookie = transaction_from(second, google_settings)
    assert old.state != new.state and old.nonce != new.nonce and old.code_verifier != new.code_verifier
    with pytest.raises(GoogleTransactionError):
        verify_google_transaction(cookie.value, old.state, google_settings)
    sessions.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 400, 429, 500])
def test_google_http_failure_issues_no_cookie_or_redirect(google_start_client, status):
    client, calls, behavior, sessions, _ = google_start_client
    behavior["status"] = status
    assert_error(client.get(PATH), 503, "temporarily_unavailable")
    assert len(calls) == 1
    sessions.assert_not_called()


@pytest.mark.parametrize("exception", [httpx.ConnectError("private connection"), httpx.ReadTimeout("private timeout")])
def test_google_connection_and_timeout_failures_are_safe(google_start_client, exception, caplog):
    client, _, behavior, sessions, _ = google_start_client
    behavior["exception"] = exception
    with caplog.at_level(logging.ERROR):
        response = client.get(PATH)
    error = assert_error(response, 503, "temporarily_unavailable")
    assert error["trace_id"] in caplog.text and "stage=google_discovery" in caplog.text
    assert "private" not in response.text and "private" not in caplog.text
    sessions.assert_not_called()


def test_total_discovery_timeout_is_enforced(google_start_client):
    client, _, behavior, sessions, _ = google_start_client
    behavior["delay"] = 0.1
    with patch("app.integrations.google_oidc.GOOGLE_DISCOVERY_TIMEOUT_SECONDS", 0.001):
        assert_error(client.get(PATH), 503, "temporarily_unavailable")
    sessions.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {},
        {**METADATA, "issuer": "https://evil.test"},
        {**METADATA, "authorization_endpoint": "https://evil.test/auth"},
        {**METADATA, "authorization_endpoint": "http://accounts.google.com/o/oauth2/v2/auth"},
        {**METADATA, "authorization_endpoint": GOOGLE_AUTHORIZATION_ENDPOINT + "?private=secret"},
    ],
)
def test_untrusted_discovery_content_is_rejected(google_start_client, metadata):
    client, calls, behavior, sessions, _ = google_start_client
    behavior["metadata"] = metadata
    assert_error(client.get(PATH), 503, "temporarily_unavailable")
    assert len(calls) == 1
    sessions.assert_not_called()


def test_non_json_discovery_response_is_rejected(google_start_client):
    client, _, behavior, _, _ = google_start_client
    behavior["content"] = b"private-not-json"
    response = client.get(PATH)
    assert_error(response, 503, "temporarily_unavailable")
    assert "private" not in response.text


@pytest.mark.parametrize("client_id", [None, "invalid-client-id"])
def test_google_settings_missing_or_invalid_does_not_block_local_auth(
    google_start_client, monkeypatch, auth_settings, client_id
):
    client, calls, _, _, app = google_start_client
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GOOGLE_TRANSACTION_KEY"):
        monkeypatch.delenv(name, raising=False)
    if client_id is not None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", client_id)
    app.state.google_settings = create_app(auth_settings).state.google_settings
    assert app.state.google_settings is None
    assert_error(client.get(PATH), 503, "temporarily_unavailable")
    assert calls == []
    assert client.get("/api/health").status_code == 200
    assert app.state.auth_settings is auth_settings
    assert client.get("/api/me").status_code == 401


def test_missing_auth_settings_prevents_discovery(google_start_client):
    client, calls, _, _, app = google_start_client
    app.state.auth_settings = None
    assert_error(client.get(PATH), 503, "temporarily_unavailable")
    assert calls == []


def test_app_loads_google_settings_once_using_same_auth_config(google_environment, auth_settings, monkeypatch):
    for name, value in google_environment.items():
        monkeypatch.setenv(name, value)
    app = create_app(auth_settings)
    assert app.state.google_settings == GoogleSettings.from_env(google_environment)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "changed-secret")
    assert app.state.google_settings.client_secret == google_environment["GOOGLE_CLIENT_SECRET"]


def test_production_start_cookie_is_secure(google_start_client, google_environment):
    client, _, _, _, app = google_start_client
    google_environment.update(
        {
            "AUTH_ENV": "production",
            "AUTH_API_ORIGIN": "https://api.example.test",
            "AUTH_ALLOWED_ORIGINS": "https://app.example.test",
            "AUTH_COOKIE_SECURE": "true",
            "GOOGLE_REDIRECT_URI": "https://api.example.test/api/auth/google/callback",
        }
    )
    app.state.auth_settings = AuthSettings.from_env(google_environment)
    app.state.google_settings = GoogleSettings.from_env(google_environment)
    response = client.get(PATH)
    _, _, cookie = transaction_from(response, app.state.google_settings)
    assert cookie["secure"] is True and cookie["httponly"] is True and cookie["domain"] == ""


def test_cookie_failure_does_not_leak_partial_success(google_start_client):
    client, _, _, _, _ = google_start_client

    def broken_cookie(response, *args):
        response.set_cookie(GOOGLE_TRANSACTION_COOKIE_NAME, "private-partial-cookie")
        raise GoogleTransactionError()

    with patch("app.api.google_auth.set_google_transaction_cookie", side_effect=broken_cookie):
        response = client.get(PATH)
    assert_error(response, 503, "temporarily_unavailable")
    assert "private-partial-cookie" not in response.text


def test_invalid_transaction_key_returns_safe_503(google_start_client):
    client, _, _, _, app = google_start_client
    app.state.google_settings = replace(app.state.google_settings, transaction_key=b"short")
    assert_error(client.get(PATH), 503, "temporarily_unavailable")


def test_openapi_documents_start_and_expected_errors(auth_settings, google_settings):
    schema = create_app(auth_settings, google_settings).openapi()
    operation = schema["paths"][PATH]["get"]
    assert {"302", "422", "503"} <= set(operation["responses"])
    assert any(parameter["name"] == "return_to" for parameter in operation["parameters"])
