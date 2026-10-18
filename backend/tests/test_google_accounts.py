"""Google account resolution boundaries and safe persistence failures."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.auth_identity import AuthIdentity
from app.models.user import User
from app.security.google_identity import VerifiedGoogleIdentity
from app.services.google_accounts import GoogleAccountUnavailable, get_or_create_google_user


@pytest.fixture
def verified_identity():
    return VerifiedGoogleIdentity(
        issuer="https://accounts.google.com", subject="fixture-google-subject", email="fixture@example.test"
    )


def test_existing_identity_returns_locked_user_without_writes(verified_identity):
    session = MagicMock(spec=Session)
    user = User(id=101, email="original@example.test", password_hash=None)
    stored = AuthIdentity(user_id=user.id, issuer=verified_identity.issuer, subject=verified_identity.subject)
    with (
        patch("app.services.google_accounts.find_google_identity", return_value=stored) as lookup,
        patch("app.services.google_accounts.find_user_for_update", return_value=user) as lock,
        patch("app.services.google_accounts.add_google_user") as insert,
    ):
        result = get_or_create_google_user(verified_identity, session)
    assert result.user is user and result.created is False
    assert user.email == "original@example.test"
    assert lookup.call_count == 2
    lookup.assert_called_with(session, verified_identity.issuer, verified_identity.subject)
    lock.assert_called_once_with(session, user.id)
    insert.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.begin_nested.assert_not_called()
    assert user.email not in repr(result) and verified_identity.subject not in repr(result)


def test_new_user_and_identity_share_savepoint_and_caller_controls_commit(verified_identity):
    session = MagicMock(spec=Session)
    user = User(id=101, email=verified_identity.email, password_hash=None)
    with (
        patch("app.services.google_accounts.find_google_identity", return_value=None),
        patch("app.services.google_accounts.add_google_user", return_value=user) as insert_user,
        patch("app.services.google_accounts.add_google_identity") as insert_identity,
    ):
        result = get_or_create_google_user(verified_identity, session)
    assert result.user is user and result.created is True
    session.begin_nested.assert_called_once()
    insert_user.assert_called_once_with(session, verified_identity.email)
    insert_identity.assert_called_once_with(session, user.id, verified_identity.issuer, verified_identity.subject)
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


@pytest.mark.parametrize(
    "target,stage",
    [
        ("find_google_identity", "google_identity_lookup"),
        ("add_google_user", "user_insert"),
        ("add_google_identity", "google_identity_insert"),
    ],
)
def test_storage_failures_are_safe_and_never_commit(verified_identity, target, stage):
    session = MagicMock(spec=Session)
    failure = OperationalError("fixture-private-SQL", None, RuntimeError("fixture-private-details"))
    with (
        patch("app.services.google_accounts.find_google_identity", return_value=None),
        patch("app.services.google_accounts.add_google_user", return_value=User(id=101)),
        patch(f"app.services.google_accounts.{target}", side_effect=failure),
    ):
        with pytest.raises(GoogleAccountUnavailable) as caught:
            get_or_create_google_user(verified_identity, session)
    assert caught.value.failure.stage == stage
    assert "fixture-private" not in str(caught.value)
    session.commit.assert_not_called()


def test_unrelated_integrity_failure_is_not_treated_as_identity_conflict(verified_identity):
    session = MagicMock(spec=Session)
    failure = IntegrityError("fixture-private-SQL", None, RuntimeError("other-constraint"))
    with (
        patch("app.services.google_accounts.find_google_identity", return_value=None) as lookup,
        patch("app.services.google_accounts.add_google_user", return_value=User(id=101)),
        patch("app.services.google_accounts.add_google_identity", side_effect=failure),
    ):
        with pytest.raises(GoogleAccountUnavailable) as caught:
            get_or_create_google_user(verified_identity, session)
    assert lookup.call_count == 1
    assert caught.value.failure.stage == "google_identity_insert"


@pytest.mark.parametrize("kind", ["missing-user", "local-user", "missing-identity", "changed-binding"])
def test_corrupt_or_changed_binding_is_never_used_for_login(verified_identity, kind):
    session = MagicMock(spec=Session)
    user = User(id=101, email=verified_identity.email, password_hash=None)
    original = AuthIdentity(user_id=user.id)
    current = original
    if kind == "missing-user":
        user = None
    elif kind == "local-user":
        user.password_hash = "fixture-local-hash"
    elif kind == "missing-identity":
        current = None
    else:
        current = AuthIdentity(user_id=202)
    with (
        patch("app.services.google_accounts.find_google_identity", side_effect=[original, current]),
        patch("app.services.google_accounts.find_user_for_update", return_value=user),
    ):
        with pytest.raises(GoogleAccountUnavailable) as caught:
            get_or_create_google_user(verified_identity, session)
    assert caught.value.failure.error_type == "GoogleAccountStateChanged"
    session.commit.assert_not_called()


def test_programming_failure_is_not_hidden_and_savepoint_exits(verified_identity):
    session = MagicMock(spec=Session)
    with (
        patch("app.services.google_accounts.find_google_identity", return_value=None),
        patch("app.services.google_accounts.add_google_user", side_effect=RuntimeError("programming failure")),
    ):
        with pytest.raises(RuntimeError, match="programming failure"):
            get_or_create_google_user(verified_identity, session)
    session.begin_nested.return_value.__exit__.assert_called_once()
    session.commit.assert_not_called()
