"""Verify complete HTTP authentication flows against guarded PostgreSQL."""

import logging
from base64 import b64encode
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError
from app.main import create_app
from app.models.conversation import Conversation, Message
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.security.login_limits import LoginFailureLimiter
from app.security.passwords import verify_password
from app.security.session_values import create_refresh_digest

pytestmark = pytest.mark.db
ORIGIN = {"Origin": "http://127.0.0.1:5173"}


@pytest.fixture
def flow_client(committed_signup_sessions, auth_settings):
    factory = committed_signup_sessions
    User.metadata.create_all(factory.kw["bind"], tables=[Conversation.__table__, Message.__table__])
    clock = [0.0]
    app = create_app(auth_settings)
    app.state.login_failure_limiter = LoginFailureLimiter(clock=lambda: clock[0])
    body = {"email": f"flow-{uuid4().hex}@example.test", "password": "fixture-only-password"}
    with (
        patch("app.db._session_factory", factory),
        TestClient(app, base_url="http://127.0.0.1:8000", client=("192.0.2.1", 123)) as client,
    ):
        yield client, factory, body, clock


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(client, body):
    response = client.post("/api/auth/register", json=body, headers=ORIGIN)
    assert response.status_code == 201
    return response


def create_conversation(client, token: str) -> str:
    response = client.post("/api/conversations", json={}, headers=bearer(token))
    assert response.status_code == 201
    return response.json()["conversation"]["id"]


def assert_own_data(client, token: str, user_id: str, conversation_id: str) -> None:
    response = client.get("/api/me", headers=bearer(token))
    assert response.status_code == 200 and response.json()["user"]["id"] == user_id
    listing = client.get("/api/conversations", headers=bearer(token))
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [conversation_id]
    assert client.get(f"/api/conversations/{conversation_id}/messages", headers=bearer(token)).status_code == 200


def test_register_login_and_relogin_preserve_http_identity_and_keep_secrets_out_of_logs(
    flow_client, auth_settings, caplog
):
    client, factory, body, _ = flow_client
    secrets = [*body.values()]
    for key in (
        auth_settings.access_key,
        auth_settings.refresh_key,
        auth_settings.refresh_digest_key,
        auth_settings.csrf_key,
    ):
        secrets.extend([repr(key), b64encode(key).decode()])
    with caplog.at_level(logging.INFO):
        registered = register(client, body)
        user_id = registered.json()["user"]["id"]
        secrets.extend(
            [registered.json()["access_token"], registered.json()["csrf_token"], registered.cookies["refresh_token"]]
        )
        assert client.get("/api/me", headers=bearer(registered.json()["access_token"])).status_code == 200
        first = client.post("/api/auth/login", json=body, headers=ORIGIN)
        assert first.status_code == 200 and first.json()["user"]["id"] == user_id
        token = first.json()["access_token"]
        conversation_id = create_conversation(client, token)
        second = client.post("/api/auth/login", json={**body, "email": f" {body['email'].upper()} "}, headers=ORIGIN)
        assert second.status_code == 200 and second.json()["user"]["id"] == user_id
        for response in (first, second):
            access = response.json()["access_token"]
            assert_own_data(client, access, user_id, conversation_id)
            assert response.cookies["refresh_token"] not in response.text
            secrets.extend([access, response.json()["csrf_token"], response.cookies["refresh_token"]])
    with factory() as reader:
        user = reader.get(User, int(user_id))
        assert verify_password(body["password"], user.password_hash)
        assert user.password_hash not in first.text and user.password_hash not in second.text
        secrets.append(user.password_hash)
        assert reader.scalar(select(func.count()).select_from(User)) == 1
        sessions = reader.scalars(select(RefreshSession).where(RefreshSession.user_id == int(user_id))).all()
        assert len(sessions) == 3
        assert all(
            len(row.token_digest) == 32 and row.revoked_at is None and row.rotated_from_id is None for row in sessions
        )
        assert {row.token_digest for row in sessions} == {
            create_refresh_digest(response.cookies["refresh_token"], auth_settings)
            for response in (registered, first, second)
        }
    assert all(secret not in caplog.text for secret in secrets)
    assert all(
        record.exc_info is None and record.stack_info is None
        for record in caplog.records
        if record.name.startswith("app.")
    )


def test_email_limit_recovers_without_losing_existing_authorization(flow_client):
    client, factory, body, clock = flow_client
    registered = register(client, body)
    user_id, original_access = registered.json()["user"]["id"], registered.json()["access_token"]
    conversation_id = create_conversation(client, original_access)
    for _ in range(5):
        response = client.post(
            "/api/auth/login", json={**body, "password": "fixture-only-wrong-password"}, headers=ORIGIN
        )
        assert response.status_code == 401 and "set-cookie" not in response.headers
    blocked = client.post("/api/auth/login", json=body, headers=ORIGIN)
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "600"
    assert "set-cookie" not in blocked.headers
    assert_own_data(client, original_access, user_id, conversation_id)
    clock[0] = 599.1
    almost = client.post("/api/auth/login", json=body, headers=ORIGIN)
    assert almost.status_code == 429 and almost.headers["retry-after"] == "1"
    clock[0] = 600
    recovered = client.post("/api/auth/login", json=body, headers=ORIGIN)
    assert recovered.status_code == 200
    assert_own_data(client, recovered.json()["access_token"], user_id, conversation_id)
    with factory() as reader:
        assert reader.scalar(select(func.count()).select_from(User)) == 1
        assert reader.scalar(select(func.count()).select_from(RefreshSession)) == 2


def test_missing_accounts_reach_shared_ip_limit_without_creating_users(flow_client):
    client, factory, body, clock = flow_client
    registered = register(client, body)
    for index in range(30):
        response = client.post(
            "/api/auth/login", json={**body, "email": f"missing-{index}-{uuid4().hex}@example.test"}, headers=ORIGIN
        )
        assert response.status_code == 401 and "set-cookie" not in response.headers
    blocked = client.post("/api/auth/login", json=body, headers=ORIGIN)
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "600"
    assert client.get("/api/me", headers=bearer(registered.json()["access_token"])).status_code == 200
    with factory() as reader:
        assert reader.scalar(select(func.count()).select_from(User)) == 1
        assert reader.scalar(select(func.count()).select_from(RefreshSession)) == 1
    clock[0] = 600
    assert client.post("/api/auth/login", json=body, headers=ORIGIN).status_code == 200


@pytest.mark.parametrize("failure_kind", ["commit", "cookie"])
def test_failed_relogin_keeps_prior_cookies_and_protected_records_usable(flow_client, auth_settings, failure_kind: str):
    client, factory, body, _ = flow_client
    registered = register(client, body)
    user_id, original_access = registered.json()["user"]["id"], registered.json()["access_token"]
    original_refresh = client.cookies["refresh_token"]
    conversation_id = create_conversation(client, original_access)

    def fail_commit(session):
        session.flush()
        raise OperationalError("private SQL", None, RuntimeError("private database"))

    def fail_cookie(response, refresh, settings):
        response.set_cookie("refresh_token", refresh.token)
        raise AuthCookieError("private cookie")

    context = (
        patch.object(Session, "commit", fail_commit)
        if failure_kind == "commit"
        else patch("app.api.auth.set_auth_cookies", side_effect=fail_cookie)
    )
    with context:
        failed = client.post("/api/auth/login", json=body, headers=ORIGIN)
    assert failed.status_code == 503 and "set-cookie" not in failed.headers and "private" not in failed.text
    assert client.cookies["refresh_token"] == original_refresh
    assert_own_data(client, original_access, user_id, conversation_id)
    with factory() as reader:
        sessions = reader.scalars(select(RefreshSession)).all()
        assert len(sessions) == 1
        assert sessions[0].token_digest == create_refresh_digest(original_refresh, auth_settings)
        assert sessions[0].revoked_at is None
