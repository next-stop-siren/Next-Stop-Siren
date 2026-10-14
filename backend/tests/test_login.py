"""Local credential checks using fake users and real Argon2 verification."""

from unittest.mock import Mock, patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.auth import LoginRequest
from app.security.passwords import PasswordHashError, hash_password
from app.services.login import InvalidCredentials, LoginUnavailable, authenticate_local_user


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password("  e\u0301한글🔑 password  ")


@pytest.fixture
def login_input() -> LoginRequest:
    return LoginRequest(email=" fixture@example.test ", password="  e\u0301한글🔑 password  ")


@pytest.fixture
def local_user(password_hash: str) -> User:
    return User(id=9007199254740993, email="fixture@example.test", password_hash=password_hash)


def test_success_returns_database_user_without_writing(local_user: User, login_input: LoginRequest) -> None:
    session = Mock(spec=Session)
    with patch("app.services.login.find_local_user_by_email", return_value=local_user) as lookup:
        result = authenticate_local_user(login_input, session)
    assert result is local_user
    assert result.id == 9007199254740993
    lookup.assert_called_once_with(session, "fixture@example.test")
    session.commit.assert_not_called()
    session.add.assert_not_called()
    session.flush.assert_not_called()
    session.rollback.assert_not_called()


@pytest.mark.parametrize("password", ["fixture-only-wrong-password", "e\u0301한글🔑 password", "  é한글🔑 password  "])
def test_wrong_or_normalized_password_is_rejected(local_user: User, password: str) -> None:
    body = LoginRequest(email=local_user.email, password=password)
    with patch("app.services.login.find_local_user_by_email", return_value=local_user):
        with pytest.raises(InvalidCredentials):
            authenticate_local_user(body, Mock(spec=Session))


@pytest.mark.parametrize("user", [None, User(id=202, email="fixture@example.test", password_hash=None)])
def test_missing_or_google_only_user_performs_dummy_verification(user: User | None, login_input: LoginRequest) -> None:
    with (
        patch("app.services.login.find_local_user_by_email", return_value=user),
        patch("app.services.login.verify_dummy_password") as dummy,
        patch("app.services.login.verify_password") as verify,
    ):
        with pytest.raises(InvalidCredentials):
            authenticate_local_user(login_input, Mock(spec=Session))
    dummy.assert_called_once_with(login_input.password)
    verify.assert_not_called()


def test_even_matching_dummy_password_cannot_authenticate_missing_user() -> None:
    body = LoginRequest(email="missing@example.test", password="dummy-local-login-password")
    with patch("app.services.login.find_local_user_by_email", return_value=None):
        with pytest.raises(InvalidCredentials):
            authenticate_local_user(body, Mock(spec=Session))


def test_missing_account_and_wrong_password_have_same_safe_failure(local_user: User, login_input: LoginRequest) -> None:
    body = LoginRequest(email=login_input.email, password="fixture-only-wrong-password")
    messages = []
    for user in (None, local_user):
        with patch("app.services.login.find_local_user_by_email", return_value=user):
            with pytest.raises(InvalidCredentials) as caught:
                authenticate_local_user(body, Mock(spec=Session))
        messages.append(str(caught.value))
    assert messages == ["로그인 정보를 확인해 주세요."] * 2


def test_corrupt_stored_hash_is_unavailable_not_invalid_credentials(login_input: LoginRequest) -> None:
    user = User(id=101, email=login_input.email, password_hash="private-corrupt-hash")
    with patch("app.services.login.find_local_user_by_email", return_value=user):
        with pytest.raises(LoginUnavailable) as caught:
            authenticate_local_user(login_input, Mock(spec=Session))
    assert str(caught.value) == "일시적으로 로그인을 처리할 수 없습니다."
    assert "private" not in str(caught.value)


def test_database_failure_is_safe_and_skips_password_verification(login_input: LoginRequest) -> None:
    failure = OperationalError("private SQL", None, RuntimeError("private database details"))
    with (
        patch("app.services.login.find_local_user_by_email", side_effect=failure),
        patch("app.services.login.verify_password") as verify,
        patch("app.services.login.verify_dummy_password") as dummy,
    ):
        with pytest.raises(LoginUnavailable) as caught:
            authenticate_local_user(login_input, Mock(spec=Session))
    assert str(caught.value) == "일시적으로 로그인을 처리할 수 없습니다."
    verify.assert_not_called()
    dummy.assert_not_called()


def test_dummy_verification_failure_is_unavailable(login_input: LoginRequest) -> None:
    with (
        patch("app.services.login.find_local_user_by_email", return_value=None),
        patch("app.services.login.verify_dummy_password", side_effect=PasswordHashError("private details")),
    ):
        with pytest.raises(LoginUnavailable):
            authenticate_local_user(login_input, Mock(spec=Session))


def test_programming_errors_are_not_hidden(login_input: LoginRequest) -> None:
    with patch("app.services.login.find_local_user_by_email", side_effect=RuntimeError("programming failure")):
        with pytest.raises(RuntimeError, match="programming failure"):
            authenticate_local_user(login_input, Mock(spec=Session))
