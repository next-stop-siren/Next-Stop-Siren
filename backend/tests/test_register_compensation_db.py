"""Post-commit signup compensation and safe logging with real PostgreSQL commits."""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError
from app.main import create_app
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.services.registration import register_local_user

pytestmark = pytest.mark.db
PATH = "/api/auth/register"


@pytest.fixture
def signup_body():
    return {"email": f"compensation-{uuid4().hex}@example.test", "password": "fixture-only-password"}


def fail_cookie(response, refresh, settings):
    # 첫 쿠키만 설정된 중간 실패도 오류 응답에 쿠키를 남기면 안 된다.
    response.set_cookie("refresh_token", refresh.token)
    raise AuthCookieError("private cookie details")


def events(caplog):
    return [record for record in caplog.records if "event=registration_" in record.getMessage()]


def assert_safe_failure(response, caplog, signup_body):
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "temporarily_unavailable"
    assert "set-cookie" not in response.headers
    trace_id = response.json()["error"]["trace_id"]
    assert all(trace_id in record.getMessage() for record in events(caplog))
    for secret in (*signup_body.values(), "private", "db.internal"):
        assert secret not in caplog.text and secret not in response.text
    assert all(record.exc_info is None and record.stack_info is None for record in events(caplog))


def test_cookie_failure_cancels_committed_rows_and_allows_retry(
    committed_signup_sessions, auth_settings, signup_body, caplog
):
    with (
        patch("app.db._session_factory", committed_signup_sessions),
        TestClient(create_app(auth_settings)) as client,
    ):
        with caplog.at_level(logging.INFO), patch("app.api.auth.set_auth_cookies", side_effect=fail_cookie):
            response = client.post(PATH, json=signup_body, headers={"Origin": auth_settings.allowed_origins[0]})
        assert_safe_failure(response, caplog, signup_body)
        assert [record.getMessage().split()[0] for record in events(caplog)] == [
            "event=registration_received",
            "event=registration_saved",
            "event=registration_cancelled",
            "event=registration_failed",
        ]
        # 별도 연결에서도 두 행이 사라져야 보상 커밋이 완료된 것이다.
        with committed_signup_sessions() as reader:
            assert reader.scalar(select(func.count()).select_from(User)) == 0
            assert reader.scalar(select(func.count()).select_from(RefreshSession)) == 0
        retried = client.post(PATH, json=signup_body, headers={"Origin": auth_settings.allowed_origins[0]})
        assert retried.status_code == 201


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_compensation_commit_failure_rolls_back_deletes_and_logs_both_failures(
    committed_signup_sessions, auth_settings, signup_body, caplog, rollback_fails
):
    original_commit = Session.commit
    original_rollback = Session.rollback
    calls = 0
    rollbacks = 0

    def commit(session):
        nonlocal calls
        calls += 1
        if calls == 2:
            session.flush()
            raise OperationalError(
                "DELETE FROM private_table",
                {"token": "private-token", "hash": "private-hash"},
                RuntimeError("password=private-secret host=db.internal"),
            )
        return original_commit(session)

    def rollback(session):
        nonlocal rollbacks
        rollbacks += 1
        if rollback_fails and rollbacks == 1:
            raise InterfaceError("private rollback SQL", None, RuntimeError("private connection details"))
        return original_rollback(session)

    with (
        patch("app.db._session_factory", committed_signup_sessions),
        patch.object(Session, "commit", commit),
        patch.object(Session, "rollback", rollback),
        patch("app.api.auth.set_auth_cookies", side_effect=fail_cookie),
        caplog.at_level(logging.INFO),
        TestClient(create_app(auth_settings)) as client,
    ):
        response = client.post(PATH, json=signup_body, headers={"Origin": auth_settings.allowed_origins[0]})
    assert_safe_failure(response, caplog, signup_body)
    _, _, compensation, cookie = events(caplog)
    assert "event=registration_compensation_failed" in compensation.getMessage()
    expected = (
        "stage=compensation_rollback error_type=InterfaceError"
        if rollback_fails
        else "stage=compensation error_type=OperationalError"
    )
    assert expected in compensation.getMessage()
    assert "stage=cookie error_type=AuthCookieError" in cookie.getMessage()
    assert not any("event=registration_cancelled" in record.getMessage() for record in events(caplog))
    assert calls == 2
    with committed_signup_sessions() as reader:
        assert reader.scalar(select(func.count()).select_from(User)) == 1
        assert reader.scalar(select(func.count()).select_from(RefreshSession)) == 1


def test_compensation_does_not_delete_another_session(committed_signup_sessions, auth_settings, signup_body, caplog):
    def cookie_failure_with_another_login(response, refresh, settings):
        with committed_signup_sessions() as other:
            user_id = other.scalar(select(User.id))
            other.add(
                RefreshSession(
                    user_id=user_id, token_digest=b"z" * 32, expires_at=datetime.now(timezone.utc) + timedelta(days=14)
                )
            )
            other.commit()
        fail_cookie(response, refresh, settings)

    with (
        patch("app.db._session_factory", committed_signup_sessions),
        patch("app.api.auth.set_auth_cookies", side_effect=cookie_failure_with_another_login),
        caplog.at_level(logging.INFO),
        TestClient(create_app(auth_settings)) as client,
    ):
        response = client.post(PATH, json=signup_body, headers={"Origin": auth_settings.allowed_origins[0]})
    assert_safe_failure(response, caplog, signup_body)
    assert any(
        "event=registration_compensation_failed" in record.getMessage()
        and "error_type=IntegrityError" in record.getMessage()
        for record in events(caplog)
    )
    assert not any("event=registration_cancelled" in record.getMessage() for record in events(caplog))
    with committed_signup_sessions() as reader:
        assert reader.scalar(select(func.count()).select_from(User)) == 1
        assert reader.scalar(select(func.count()).select_from(RefreshSession)) == 2


def test_cancellation_preserves_existing_accounts(committed_signup_sessions, auth_settings, signup_body, caplog):
    with committed_signup_sessions() as session:
        existing = register_local_user(
            RegisterRequest(email=f"existing-{uuid4().hex}@example.test", password="fixture-only-existing-password"),
            session,
            auth_settings,
        )
        google = User(email=signup_body["email"], password_hash=None)
        session.add(google)
        session.commit()
        google_id = google.id
    with (
        patch("app.db._session_factory", committed_signup_sessions),
        patch("app.api.auth.set_auth_cookies", side_effect=fail_cookie),
        caplog.at_level(logging.INFO),
        TestClient(create_app(auth_settings)) as client,
    ):
        response = client.post(PATH, json=signup_body, headers={"Origin": auth_settings.allowed_origins[0]})
    assert_safe_failure(response, caplog, signup_body)
    with committed_signup_sessions() as reader:
        assert reader.scalar(select(func.count()).select_from(User)) == 2
        assert reader.scalar(select(func.count()).select_from(RefreshSession)) == 1
        assert reader.get(User, int(existing.user.id)) is not None
        assert reader.get(User, google_id).password_hash is None
