"""Credential lookup semantics against the guarded test PostgreSQL database."""

from uuid import uuid4

import pytest

from app.models.user import User
from app.schemas.auth import LoginRequest
from app.security.passwords import hash_password
from app.services.login import InvalidCredentials, LoginUnavailable, authenticate_local_user

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password("fixture-only-password")


def test_case_insensitive_local_lookup_preserves_separate_google_user(orm_test_session, password_hash: str) -> None:
    email = f"login-{uuid4().hex}@example.test"
    local = User(email=email, password_hash=password_hash)
    google = User(email=email, password_hash=None)
    orm_test_session.add_all([google, local])
    orm_test_session.flush()

    body = LoginRequest(email=f" {email.upper()} ", password="fixture-only-password")
    result = authenticate_local_user(body, orm_test_session)

    assert result.id == local.id
    assert result.id != google.id
    assert result.password_hash == password_hash
    assert google.password_hash is None
    assert not orm_test_session.new and not orm_test_session.dirty and not orm_test_session.deleted


@pytest.mark.parametrize("account_kind", ["missing", "google", "local"])
def test_missing_google_only_and_wrong_password_all_fail(
    orm_test_session, password_hash: str, account_kind: str
) -> None:
    email = f"login-{uuid4().hex}@example.test"
    if account_kind != "missing":
        orm_test_session.add(User(email=email, password_hash=password_hash if account_kind == "local" else None))
        orm_test_session.flush()
    body = LoginRequest(email=email, password="fixture-only-wrong-password")
    with pytest.raises(InvalidCredentials, match="로그인 정보를 확인해 주세요."):
        authenticate_local_user(body, orm_test_session)
    assert not orm_test_session.new and not orm_test_session.dirty and not orm_test_session.deleted


def test_corrupt_database_hash_is_an_internal_failure(orm_test_session) -> None:
    email = f"login-{uuid4().hex}@example.test"
    orm_test_session.add(User(email=email, password_hash="fixture-corrupt-hash"))
    orm_test_session.flush()
    body = LoginRequest(email=email, password="fixture-only-password")
    with pytest.raises(LoginUnavailable):
        authenticate_local_user(body, orm_test_session)
