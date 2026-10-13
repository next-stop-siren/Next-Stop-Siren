"""Signup persistence and failure rollback against guarded PostgreSQL."""

from unittest.mock import patch
from uuid import uuid4

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models.auth_identity import AuthIdentity
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.security.passwords import PasswordHashError, verify_password
from app.security.session_values import create_refresh_digest
from app.security.tokens import TokenIssueError
from app.services.registration import LocalEmailExists, RegistrationUnavailable, register_local_user

pytestmark = pytest.mark.db


@pytest.fixture
def signup_input() -> RegisterRequest:
    return RegisterRequest(email=f"signup-{uuid4().hex}@example.test", password="fixture-only-password")


def user_count(session, email: str) -> int:
    return session.scalar(select(func.count()).select_from(User).where(func.lower(User.email) == func.lower(email)))


def refresh_count(session) -> int:
    return session.scalar(select(func.count()).select_from(RefreshSession))


def test_signup_commits_user_hash_and_only_refresh_digest(orm_test_session, auth_settings, signup_input) -> None:
    result = register_local_user(signup_input, orm_test_session, auth_settings)
    assert not orm_test_session.in_transaction()
    user = orm_test_session.get(User, int(result.user.id))
    assert user.email == signup_input.email
    assert user.password_hash != signup_input.password
    assert verify_password(signup_input.password, user.password_hash)
    assert result.user.model_dump(mode="json")["id"] == str(user.id)
    assert set(result.user.model_dump()) == {"id", "email", "created_at"}
    assert "password" not in repr(result)
    refresh = orm_test_session.scalar(select(RefreshSession).where(RefreshSession.user_id == user.id))
    assert refresh.token_digest == create_refresh_digest(result.refresh.token, auth_settings)
    assert len(refresh.token_digest) == 32
    assert refresh.issued_at == result.refresh.issued_at
    assert refresh.expires_at == result.refresh.expires_at
    assert refresh.rotated_from_id is None
    assert refresh.revoked_at is None
    assert result.refresh.token not in repr(result)
    assert result.access.token not in repr(result)
    assert (
        orm_test_session.scalar(select(func.count()).select_from(AuthIdentity).where(AuthIdentity.user_id == user.id))
        == 0
    )
    for issued, key in ((result.access, auth_settings.access_key), (result.refresh, auth_settings.refresh_key)):
        claims = jwt.decode(
            issued.token, key, algorithms=["HS256"], audience=auth_settings.audience, issuer=auth_settings.issuer
        )
        assert claims["sub"] == result.user.id
        assert claims["token_type"] == issued.token_type


def test_case_and_trim_duplicate_does_not_create_another_session(orm_test_session, auth_settings, signup_input) -> None:
    first = register_local_user(signup_input, orm_test_session, auth_settings)
    sessions_before = refresh_count(orm_test_session)
    duplicate = RegisterRequest(email=f" {signup_input.email.upper()} ", password="different-fixture-password")
    with pytest.raises(LocalEmailExists):
        register_local_user(duplicate, orm_test_session, auth_settings)
    assert user_count(orm_test_session, signup_input.email) == 1
    assert refresh_count(orm_test_session) == sessions_before
    assert verify_password(signup_input.password, orm_test_session.get(User, int(first.user.id)).password_hash)


def test_google_account_with_same_email_remains_separate(orm_test_session, auth_settings, signup_input) -> None:
    google = User(email=signup_input.email, password_hash=None)
    orm_test_session.add(google)
    orm_test_session.flush()
    google_id = google.id
    orm_test_session.add(AuthIdentity(user_id=google_id, issuer="https://accounts.google.com", subject=uuid4().hex))
    orm_test_session.commit()
    result = register_local_user(signup_input, orm_test_session, auth_settings)
    assert int(result.user.id) != google_id
    assert user_count(orm_test_session, signup_input.email) == 2
    assert orm_test_session.get(User, google_id).password_hash is None
    assert orm_test_session.scalar(select(AuthIdentity.user_id).where(AuthIdentity.user_id == google_id)) == google_id


def test_final_unique_index_handles_a_missed_precheck(orm_test_session, auth_settings, signup_input) -> None:
    register_local_user(signup_input, orm_test_session, auth_settings)
    sessions_before = refresh_count(orm_test_session)
    with patch("app.services.registration.find_local_user_by_email", return_value=None):
        with pytest.raises(LocalEmailExists):
            register_local_user(signup_input, orm_test_session, auth_settings)
    assert user_count(orm_test_session, signup_input.email) == 1
    assert refresh_count(orm_test_session) == sessions_before


def test_refresh_constraint_failure_rolls_back_user_and_is_not_email_conflict(
    orm_test_session, auth_settings, signup_input
) -> None:
    sessions_before = refresh_count(orm_test_session)
    with patch("app.services.registration.create_refresh_digest", return_value=b"short"):
        with pytest.raises(RegistrationUnavailable) as caught:
            register_local_user(signup_input, orm_test_session, auth_settings)
    assert str(caught.value) == "가입 저장을 완료할 수 없습니다."
    assert user_count(orm_test_session, signup_input.email) == 0
    assert refresh_count(orm_test_session) == sessions_before


@pytest.mark.parametrize(
    "target,failure",
    [
        ("hash_password", PasswordHashError("private hashing error")),
        ("issue_access_token", TokenIssueError("private access error")),
        ("issue_refresh_token", TokenIssueError("private refresh error")),
    ],
)
def test_credential_failure_returns_no_result_and_rolls_back(
    orm_test_session, auth_settings, signup_input, target, failure
):
    sessions_before = refresh_count(orm_test_session)
    with patch(f"app.services.registration.{target}", side_effect=failure):
        with pytest.raises(RegistrationUnavailable) as caught:
            register_local_user(signup_input, orm_test_session, auth_settings)
    assert "private" not in str(caught.value)
    assert user_count(orm_test_session, signup_input.email) == 0
    assert refresh_count(orm_test_session) == sessions_before


def test_failure_before_commit_rolls_back_flushed_rows(orm_test_session, auth_settings, signup_input) -> None:
    sessions_before = refresh_count(orm_test_session)

    def fail_commit() -> None:
        orm_test_session.flush()
        raise OperationalError("private SQL", None, RuntimeError("private connection details"))

    with patch.object(orm_test_session, "commit", side_effect=fail_commit):
        with pytest.raises(RegistrationUnavailable) as caught:
            register_local_user(signup_input, orm_test_session, auth_settings)
    assert "private" not in str(caught.value)
    assert user_count(orm_test_session, signup_input.email) == 0
    assert refresh_count(orm_test_session) == sessions_before


def test_database_query_failure_is_safe_and_leaves_session_usable(
    orm_test_session, auth_settings, signup_input
) -> None:
    with patch.object(
        orm_test_session, "scalar", side_effect=OperationalError("private SQL", None, RuntimeError("private"))
    ):
        with pytest.raises(RegistrationUnavailable) as caught:
            register_local_user(signup_input, orm_test_session, auth_settings)
    assert "private" not in str(caught.value)
    assert user_count(orm_test_session, signup_input.email) == 0


def test_programming_failure_after_user_flush_is_rolled_back_and_not_hidden(
    orm_test_session, auth_settings, signup_input
) -> None:
    with patch("app.services.registration.issue_access_token", side_effect=RuntimeError("programming failure")):
        with pytest.raises(RuntimeError, match="programming failure"):
            register_local_user(signup_input, orm_test_session, auth_settings)
    assert user_count(orm_test_session, signup_input.email) == 0
