"""Committed login sessions, compensation, and concurrency in guarded PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import logging
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError
from app.main import create_app
from app.models.conversation import Conversation
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest
from app.security.session_values import create_csrf_token, create_refresh_digest
from app.security.tokens import TokenIssueError
from app.services.login import LoginUnavailable, authenticate_local_user, cancel_login, create_login_session
from app.services.registration import register_local_user

pytestmark = pytest.mark.db
PATH = "/api/auth/login"
HEADERS = {"Origin": "http://127.0.0.1:5173"}


@pytest.fixture
def login_database(committed_signup_sessions, auth_settings):
    factory = committed_signup_sessions
    Conversation.__table__.create(factory.kw["bind"])
    body = {"email": f"login-{uuid4().hex}@example.test", "password": "fixture-only-password"}
    with factory() as session:
        registered = register_local_user(RegisterRequest.model_validate(body), session, auth_settings)
        user_id = int(registered.user.id)
        conversation = Conversation(user_id=user_id)
        session.add(conversation)
        session.commit()
        conversation_id = conversation.id
    yield factory, body, user_id, create_refresh_digest(registered.refresh.token, auth_settings), conversation_id


@pytest.fixture
def db_client(login_database, auth_settings):
    factory, _, _, _, _ = login_database
    with (
        patch("app.db._session_factory", factory),
        TestClient(create_app(auth_settings), base_url="http://127.0.0.1:8000", client=("192.0.2.1", 123)) as client,
    ):
        yield client


def assert_preserved(login_database, *, sessions: int):
    factory, _, user_id, original_digest, conversation_id = login_database
    with factory() as reader:
        assert reader.get(User, user_id) is not None
        assert reader.get(Conversation, conversation_id).user_id == user_id
        original = reader.scalar(select(RefreshSession).where(RefreshSession.token_digest == original_digest))
        assert original is not None and original.revoked_at is None and original.rotated_from_id is None
        assert reader.scalar(select(func.count()).select_from(RefreshSession)) == sessions


def test_relogin_returns_valid_credentials_and_preserves_existing_data(db_client, login_database, auth_settings):
    factory, body, user_id, _, _ = login_database
    responses = [
        db_client.post(PATH, json={**body, "email": f" {body['email'].upper()} "}, headers=HEADERS) for _ in range(2)
    ]
    refresh_jtis = []
    for response in responses:
        assert response.status_code == 200
        data = response.json()
        assert data["user"]["id"] == str(user_id)
        access = jwt.decode(
            data["access_token"],
            auth_settings.access_key,
            algorithms=["HS256"],
            issuer=auth_settings.issuer,
            audience=auth_settings.audience,
        )
        refresh_token = response.cookies["refresh_token"]
        refresh = jwt.decode(
            refresh_token,
            auth_settings.refresh_key,
            algorithms=["HS256"],
            issuer=auth_settings.issuer,
            audience=auth_settings.audience,
        )
        assert access["sub"] == refresh["sub"] == str(user_id)
        assert access["token_type"] == "access" and access["exp"] - access["iat"] == 900
        assert refresh["token_type"] == "refresh" and refresh["exp"] - refresh["iat"] == 14 * 24 * 60 * 60
        assert data["csrf_token"] == response.cookies["csrf_token"] == create_csrf_token(refresh["jti"], auth_settings)
        refresh_jtis.append(refresh["jti"])
        with factory() as reader:
            stored = reader.scalar(
                select(RefreshSession).where(
                    RefreshSession.token_digest == create_refresh_digest(refresh_token, auth_settings)
                )
            )
            assert stored.user_id == user_id and stored.rotated_from_id is None and stored.revoked_at is None
            assert len(stored.token_digest) == 32
            assert stored.expires_at - stored.issued_at == timedelta(days=14)
            assert refresh_token not in response.text
    assert len(set(refresh_jtis)) == 2
    assert_preserved(login_database, sessions=3)


def fail_cookie(response, refresh, settings):
    response.set_cookie("refresh_token", refresh.token)
    raise AuthCookieError("private cookie details")


def test_cookie_failure_cancels_only_new_session_and_can_retry(db_client, login_database, caplog):
    _, body, _, _, _ = login_database
    with patch("app.api.auth.set_auth_cookies", side_effect=fail_cookie), caplog.at_level(logging.INFO):
        response = db_client.post(PATH, json=body, headers=HEADERS)
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert "event=login_cancelled" in caplog.text
    assert body["email"] not in caplog.text and body["password"] not in caplog.text and "private" not in caplog.text
    assert_preserved(login_database, sessions=1)
    assert db_client.post(PATH, json=body, headers=HEADERS).status_code == 200
    assert_preserved(login_database, sessions=2)


def test_compensation_preserves_another_login_created_during_cookie_failure(db_client, login_database, auth_settings):
    factory, body, _, _, _ = login_database

    def another_login_then_fail(response, refresh, settings):
        with factory() as other:
            create_login_session(LoginRequest.model_validate(body), other, auth_settings)
        fail_cookie(response, refresh, settings)

    with patch("app.api.auth.set_auth_cookies", side_effect=another_login_then_fail):
        response = db_client.post(PATH, json=body, headers=HEADERS)
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert_preserved(login_database, sessions=2)


def test_commit_failure_leaves_no_new_session_or_credentials(db_client, login_database):
    _, body, _, _, _ = login_database

    def failing_commit(session):
        session.flush()
        raise OperationalError("private SQL", None, RuntimeError("private database"))

    with patch.object(Session, "commit", failing_commit):
        response = db_client.post(PATH, json=body, headers=HEADERS)
    assert response.status_code == 503 and "set-cookie" not in response.headers and "private" not in response.text
    assert_preserved(login_database, sessions=1)


def test_token_issue_failure_does_not_change_existing_data(db_client, login_database):
    _, body, _, _, _ = login_database
    with patch("app.services.login.issue_refresh_token", side_effect=TokenIssueError("private")):
        response = db_client.post(PATH, json=body, headers=HEADERS)
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert_preserved(login_database, sessions=1)


def test_compensation_commit_failure_is_reported_and_does_not_delete_existing_data(db_client, login_database, caplog):
    _, body, _, _, _ = login_database
    original_commit = Session.commit
    calls = 0

    def commit(session):
        nonlocal calls
        calls += 1
        if calls == 2:
            session.flush()
            raise OperationalError("private DELETE", None, RuntimeError("private database"))
        return original_commit(session)

    with (
        patch.object(Session, "commit", commit),
        patch("app.api.auth.set_auth_cookies", side_effect=fail_cookie),
        caplog.at_level(logging.INFO),
    ):
        response = db_client.post(PATH, json=body, headers=HEADERS)
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert "event=login_compensation_failed" in caplog.text and "event=login_cancelled" not in caplog.text
    assert "private" not in caplog.text
    assert_preserved(login_database, sessions=2)


def test_wrong_password_and_missing_account_create_no_session(db_client, login_database):
    _, body, _, _, _ = login_database
    for request in ({**body, "password": "fixture-only-wrong-password"}, {**body, "email": "missing@example.test"}):
        response = db_client.post(PATH, json=request, headers=HEADERS)
        assert response.status_code == 401 and "set-cookie" not in response.headers
    assert_preserved(login_database, sessions=1)


def test_concurrent_logins_create_independent_roots(login_database, auth_settings):
    factory, body, _, _, _ = login_database
    barrier = Barrier(2)

    def authenticate(request, session):
        user = authenticate_local_user(request, session)
        barrier.wait(timeout=10)
        return user

    def login():
        with factory() as session:
            return create_login_session(LoginRequest.model_validate(body), session, auth_settings)

    with (
        patch("app.services.login.authenticate_local_user", side_effect=authenticate),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(login) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0].user.id == results[1].user.id
    assert results[0].refresh.jti != results[1].refresh.jti
    assert_preserved(login_database, sessions=3)


def test_cancellation_is_idempotent(login_database, auth_settings):
    factory, body, _, _, _ = login_database
    with factory() as session:
        result = create_login_session(LoginRequest.model_validate(body), session, auth_settings)
        cancel_login(result, session, auth_settings)
        cancel_login(result, session, auth_settings)
    assert_preserved(login_database, sessions=1)


def test_cancellation_refuses_a_revoked_session(login_database, auth_settings):
    factory, body, _, _, _ = login_database
    with factory() as session:
        result = create_login_session(LoginRequest.model_validate(body), session, auth_settings)
        refresh = session.scalar(
            select(RefreshSession).where(
                RefreshSession.token_digest == create_refresh_digest(result.refresh.token, auth_settings)
            )
        )
        refresh.revoked_at = result.refresh.issued_at
        session.commit()
        with pytest.raises(LoginUnavailable) as caught:
            cancel_login(result, session, auth_settings)
        assert caught.value.failure.error_type == "LoginStateChanged"
    assert_preserved(login_database, sessions=2)
