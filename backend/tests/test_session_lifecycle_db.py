"""Registration, local login, real OIDC verification, rotation, logout, and recovery in PostgreSQL."""

from base64 import b64encode
from http.cookies import SimpleCookie
import logging
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError
from app.api.google_cookies import GOOGLE_TRANSACTION_COOKIE_NAME
from app.main import create_app
from app.models.auth_identity import AuthIdentity
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.security.session_values import create_refresh_digest
from app.security.tokens import verify_refresh_token

pytestmark = pytest.mark.db
ORIGIN = "http://127.0.0.1:5173"


def post_headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["csrf_token"]}


def current_access(client, expected_user_id):
    csrf = client.get("/api/auth/csrf")
    assert csrf.status_code == 200 and "set-cookie" not in csrf.headers
    refreshed = client.post(
        "/api/auth/refresh",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf.json()["csrf_token"],
        },
    )
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["csrf_token"] == client.cookies["csrf_token"]
    assert client.cookies["refresh_token"] not in refreshed.text
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["user"]["id"] == expected_user_id
    return body["access_token"]


def logout_and_check(client, access):
    logged_out = client.post("/api/auth/logout", headers=post_headers(client))
    assert logged_out.status_code == 204 and logged_out.content == b""
    cookies = SimpleCookie()
    for header in logged_out.headers.get_list("set-cookie"):
        cookies.load(header)
    assert set(cookies) == {"refresh_token", "csrf_token"}
    assert all(cookie["max-age"] == "0" for cookie in cookies.values())
    assert "refresh_token" not in client.cookies and "csrf_token" not in client.cookies
    assert client.get("/api/auth/csrf").status_code == 401
    assert client.get("/api/me", headers={"Authorization": f"Bearer {access}"}).status_code == 200


@pytest.mark.parametrize("entry", ["register", "login"])
def test_local_entry_through_logout_keeps_absolute_expiry_and_secrets_out_of_logs(
    committed_signup_sessions, auth_settings, entry, caplog
):
    factory = committed_signup_sessions
    credentials = {"email": f"s06-{uuid4().hex}@example.test", "password": "fixture-only-password"}
    secrets = list(credentials.values())
    for key in (
        auth_settings.access_key,
        auth_settings.refresh_key,
        auth_settings.refresh_digest_key,
        auth_settings.csrf_key,
    ):
        secrets.extend([repr(key), b64encode(key).decode()])
    with (
        patch("app.db._session_factory", factory),
        TestClient(create_app(auth_settings), base_url="http://127.0.0.1:8000", client=("192.0.2.1", 123)) as client,
        caplog.at_level(logging.INFO),
    ):
        registered = client.post("/api/auth/register", json=credentials, headers={"Origin": ORIGIN})
        assert registered.status_code == 201
        secrets.extend(
            [registered.cookies["refresh_token"], registered.json()["access_token"], registered.json()["csrf_token"]]
        )
        entry_response = registered
        if entry == "login":
            entry_response = client.post("/api/auth/login", json=credentials, headers={"Origin": ORIGIN})
            assert entry_response.status_code == 200
        user_id = entry_response.json()["user"]["id"]
        root_token = client.cookies["refresh_token"]
        root_expiry = verify_refresh_token(root_token, auth_settings).expires_at
        secrets.extend([root_token, entry_response.json()["csrf_token"], entry_response.json()["access_token"]])
        jtis = {verify_refresh_token(root_token, auth_settings).jti}
        for _ in range(2):
            access = current_access(client, user_id)
            token = client.cookies["refresh_token"]
            verified = verify_refresh_token(token, auth_settings)
            assert verified.expires_at == root_expiry and verified.jti not in jtis
            jtis.add(verified.jti)
            secrets.extend([token, access, client.cookies["csrf_token"]])
        logout_and_check(client, access)
        relogin = client.post("/api/auth/login", json=credentials, headers={"Origin": ORIGIN})
        assert relogin.status_code == 200 and relogin.json()["user"]["id"] == user_id
        secrets.extend([client.cookies["refresh_token"], relogin.json()["access_token"], relogin.json()["csrf_token"]])
        with factory() as reader:
            rows = list(reader.scalars(select(RefreshSession).where(RefreshSession.user_id == int(user_id))))
            assert len(rows) == (4 if entry == "register" else 5)
            assert all(len(row.token_digest) == 32 for row in rows)
            by_id = {row.id: row for row in rows}
            for row in rows:
                if row.rotated_from_id is not None:
                    assert row.user_id == by_id[row.rotated_from_id].user_id
                    assert row.expires_at == root_expiry
            root = next(row for row in rows if row.token_digest == create_refresh_digest(root_token, auth_settings))
            assert root.revoked_at is not None
            assert reader.scalar(select(func.count()).select_from(User)) == 1
            secrets.append(reader.get(User, int(user_id)).password_hash)
    assert all(secret not in caplog.text for secret in secrets)
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("app."))


def test_oidc_callback_through_refresh_logout_preserves_independent_google_login(
    google_callback_flow, committed_google_sessions, auth_settings, caplog
):
    flow, factory = google_callback_flow, committed_google_sessions
    with patch("app.db._session_factory", factory), caplog.at_level(logging.INFO):
        first = flow.client.get("/api/auth/google/callback", params=flow.params)
        assert first.status_code == 302 and first.headers["location"] == "/"
        assert "access_token" not in first.text and "access" not in first.headers["location"]
        token = flow.client.cookies["refresh_token"]
        user_id = str(verify_refresh_token(token, auth_settings).user_id)
        access = current_access(flow.client, user_id)
        # 첫 브라우저의 쿠키를 메모리에 보관하고 독립 로그인을 만든다.
        first_cookies = list(flow.client.cookies.jar)
        flow.start(code="fixture-s06-second-code")
        second = flow.client.get("/api/auth/google/callback", params=flow.params)
        assert second.status_code == 302
        independent_digest = create_refresh_digest(flow.client.cookies["refresh_token"], auth_settings)
        with TestClient(flow.app, base_url=auth_settings.api_origin) as original_client:
            for cookie in first_cookies:
                original_client.cookies.jar.set_cookie(cookie)
            logout_and_check(original_client, access)
        assert flow.client.get("/api/auth/csrf").status_code == 200
        with factory() as reader:
            assert reader.scalar(select(func.count()).select_from(User)) == 1
            assert reader.scalar(select(func.count()).select_from(AuthIdentity)) == 1
            rows = list(reader.scalars(select(RefreshSession)))
            assert len(rows) == 3
            assert all(row.revoked_at is not None for row in rows if row.token_digest != independent_digest)
            assert next(row for row in rows if row.token_digest == independent_digest).revoked_at is None
        assert GOOGLE_TRANSACTION_COOKIE_NAME not in flow.client.cookies
        assert token not in caplog.text and access not in caplog.text and flow.id_token not in caplog.text


@pytest.mark.parametrize("failure", ["commit", "cookie"])
def test_refresh_failure_recovers_through_retry_or_relogin(committed_signup_sessions, auth_settings, failure):
    factory = committed_signup_sessions
    credentials = {"email": f"recover-{uuid4().hex}@example.test", "password": "fixture-only-password"}
    with (
        patch("app.db._session_factory", factory),
        TestClient(create_app(auth_settings), base_url="http://127.0.0.1:8000", client=("192.0.2.1", 123)) as client,
    ):
        registered = client.post("/api/auth/register", json=credentials, headers={"Origin": ORIGIN})
        assert registered.status_code == 201
        user_id = registered.json()["user"]["id"]
        old_cookie = client.cookies["refresh_token"]

        def failed_commit(session):
            session.flush()
            raise OperationalError("private SQL", None, None)

        injected = (
            patch.object(Session, "commit", failed_commit)
            if failure == "commit"
            else patch("app.api.cookie_sessions.set_auth_cookies", side_effect=AuthCookieError("private cookie"))
        )
        with injected:
            failed = client.post("/api/auth/refresh", headers=post_headers(client))
        assert failed.status_code == 503 and "set-cookie" not in failed.headers
        assert client.cookies["refresh_token"] == old_cookie
        csrf = client.get("/api/auth/csrf")
        assert csrf.status_code == (200 if failure == "commit" else 401)
        if failure == "cookie":
            logged_in = client.post("/api/auth/login", json=credentials, headers={"Origin": ORIGIN})
            assert logged_in.status_code == 200 and logged_in.json()["user"]["id"] == user_id
        access = current_access(client, user_id)
        logout_and_check(client, access)
        with factory() as reader:
            assert reader.scalar(select(func.count()).select_from(User)) == 1
            assert all(row.revoked_at is not None for row in reader.scalars(select(RefreshSession)))
