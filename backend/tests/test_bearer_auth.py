"""Verify real Bearer authentication, current-user responses, and Swagger metadata."""

import base64
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.auth import authenticated_session
from app.db import get_session
from app.main import create_app
from app.models.user import User
from app.security.tokens import issue_access_token, issue_refresh_token

BIG_ID = 9007199254740993


@pytest.fixture
def protected_client(auth_settings):
    app = create_app(auth_settings)
    session = Mock(spec=Session)
    user = User(
        id=BIG_ID, email="fixture@example.test", password_hash="private-hash", created_at=datetime.now(timezone.utc)
    )
    session.get.return_value = user
    opened = Mock()

    def db_session():
        opened()
        yield session

    app.dependency_overrides[authenticated_session] = db_session
    app.dependency_overrides[get_session] = db_session
    token = issue_access_token(BIG_ID, auth_settings).token
    with TestClient(app) as client:
        yield client, session, user, token, opened


def assert_401(response) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "set-cookie" not in response.headers
    error = response.json()["error"]
    assert error["code"] == "unauthenticated" and error["fields"] == {} and error["trace_id"]
    assert "private" not in response.text


def test_me_returns_only_public_user_with_precise_bigint(protected_client) -> None:
    client, session, user, token, opened = protected_client
    response = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    public = response.json()["user"]
    assert set(response.json()) == {"user"}
    assert set(public) == {"id", "email", "created_at"}
    assert public["id"] == str(BIG_ID) and public["email"] == user.email
    assert public["created_at"].endswith("Z")
    assert user.password_hash not in response.text
    assert token not in response.text
    assert response.headers["cache-control"] == "no-store"
    session.get.assert_called_once_with(User, BIG_ID)
    opened.assert_called_once()


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_scheme_is_case_insensitive(protected_client, scheme: str) -> None:
    client, _, _, token, _ = protected_client
    assert client.get("/api/me", headers={"Authorization": f"{scheme} {token}"}).status_code == 200


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic fixture", "Token fixture", "Bearer a.b.c", "Bearer fixture.access.a.jwt"],
)
@pytest.mark.parametrize("path", ["/api/me", "/api/conversations", "/api/conversations/501/messages"])
def test_missing_and_malformed_credentials_never_open_database(protected_client, header, path: str) -> None:
    client, session, _, _, opened = protected_client
    response = client.get(path, headers={} if header is None else {"Authorization": header})
    assert_401(response)
    opened.assert_not_called()
    session.get.assert_not_called()


@pytest.mark.parametrize("variant", ["double-space", "trailing-space", "comma", "duplicate"])
def test_ambiguous_authorization_headers_are_rejected(protected_client, variant: str) -> None:
    client, _, _, token, opened = protected_client
    if variant == "duplicate":
        headers = [("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")]
    else:
        value = {
            "double-space": f"Bearer  {token}",
            "trailing-space": f"Bearer {token} ",
            "comma": f"Bearer {token},Bearer {token}",
        }[variant]
        headers = {"Authorization": value}
    assert_401(client.get("/api/me", headers=headers))
    opened.assert_not_called()


def test_refresh_cookies_cannot_authenticate(protected_client, auth_settings) -> None:
    client, _, _, _, opened = protected_client
    refresh = issue_refresh_token(BIG_ID, auth_settings).token
    assert_401(client.get("/api/me", headers={"Cookie": f"refresh_token={refresh}; csrf_token=fixture-csrf"}))
    opened.assert_not_called()


@pytest.mark.parametrize("kind", ["expired", "future", "refresh", "forged"])
@pytest.mark.parametrize("path", ["/api/me", "/api/conversations"])
def test_invalid_tokens_are_rejected_by_real_protected_routes(
    protected_client, auth_settings, kind: str, path: str
) -> None:
    client, _, _, token, opened = protected_client
    if kind == "expired":
        token = issue_access_token(BIG_ID, auth_settings, now=datetime.now(timezone.utc) - timedelta(seconds=900)).token
    elif kind == "future":
        token = issue_access_token(BIG_ID, auth_settings, now=datetime.now(timezone.utc) + timedelta(seconds=60)).token
    elif kind == "refresh":
        token = issue_refresh_token(BIG_ID, auth_settings).token
    else:
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["sub"] = "202"
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        token = f"{header}.{payload}.{signature}"
    assert_401(client.get(path, headers={"Authorization": f"Bearer {token}"}))
    opened.assert_not_called()


def test_query_user_id_cannot_replace_token_subject(protected_client) -> None:
    client, session, _, token, _ = protected_client
    response = client.get("/api/me?user_id=202", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200 and response.json()["user"]["id"] == str(BIG_ID)
    session.get.assert_called_once_with(User, BIG_ID)


def test_deleted_user_is_unauthenticated(protected_client) -> None:
    client, session, _, token, _ = protected_client
    session.get.return_value = None
    assert_401(client.get("/api/me", headers={"Authorization": f"Bearer {token}"}))


def test_real_bearer_subject_reaches_conversation_repository(protected_client) -> None:
    client, _, _, token, _ = protected_client
    with patch("app.api.conversations.repository.list_conversations", return_value=[]) as listing:
        response = client.get("/api/conversations", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200 and response.json() == {"items": [], "next_cursor": None}
    assert listing.call_args.args[1] == BIG_ID


def test_authenticated_body_cannot_supply_another_user_id(protected_client) -> None:
    client, _, _, token, _ = protected_client
    with patch("app.api.conversations.repository.create_conversation") as create:
        response = client.post(
            "/api/conversations", json={"user_id": "202"}, headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 422
    create.assert_not_called()


def test_me_database_failure_is_safe(protected_client, caplog) -> None:
    client, session, _, token, _ = protected_client
    session.get.side_effect = OperationalError("private SQL", None, RuntimeError("private connection"))
    response = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "temporarily_unavailable"
    assert "private" not in response.text and "private" not in caplog.text and token not in caplog.text


def test_me_session_initialization_failure_is_safe(auth_settings) -> None:
    def failing_session():
        raise RuntimeError("private DATABASE_URL")
        yield

    token = issue_access_token(101, auth_settings).token
    with patch("app.api.auth.get_session", failing_session), TestClient(create_app(auth_settings)) as client:
        response = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503 and "private" not in response.text and "DATABASE_URL" not in response.text


def test_missing_auth_settings_keep_missing_token_401_and_block_valid_header(monkeypatch, auth_settings) -> None:
    monkeypatch.delenv("AUTH_ACCESS_KEY", raising=False)
    token = issue_access_token(101, auth_settings).token
    with patch("app.api.auth.get_session") as sessions, TestClient(create_app()) as client:
        assert_401(client.get("/api/me"))
        assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 503
    sessions.assert_not_called()


def test_swagger_describes_bearer_for_protected_routes_only(auth_settings) -> None:
    schema = create_app(auth_settings).openapi()
    scheme = schema["components"]["securitySchemes"]["AccessBearer"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer" and scheme["bearerFormat"] == "JWT"
    paths = schema["paths"]
    for path, method in [
        ("/api/me", "get"),
        ("/api/conversations", "get"),
        ("/api/conversations", "post"),
        ("/api/conversations/{conversation_id}/messages", "get"),
    ]:
        assert paths[path][method]["security"] == [{"AccessBearer": []}]
    for path in ("/api/auth/login", "/api/auth/register", "/api/health"):
        method = "get" if path == "/api/health" else "post"
        assert not paths[path][method].get("security")
