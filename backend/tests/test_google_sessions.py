"""Google session commit boundaries and safe failure propagation without HTTP or a database."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.user import User
from app.observability import FailureDetails
from app.security.google_identity import VerifiedGoogleIdentity
from app.security.session_values import create_refresh_digest
from app.security.tokens import TokenIssueError
from app.services.google_accounts import GoogleAccountUnavailable, GoogleUserResult
from app.services.google_sessions import cancel_google_login, create_google_login_session
from app.services.login import LoginUnavailable


@pytest.fixture
def identity():
    return VerifiedGoogleIdentity(
        issuer="https://accounts.google.com", subject="fixture-subject", email="fixture@example.test"
    )


@pytest.fixture
def account(identity):
    return GoogleUserResult(
        user=User(id=101, email=identity.email, password_hash=None, created_at=datetime.now(timezone.utc)),
        created=True,
    )


def test_commit_precedes_result_and_only_refresh_digest_is_persisted(identity, account, auth_settings):
    session = MagicMock(spec=Session)
    with (
        patch("app.services.google_sessions.get_or_create_google_user", return_value=account) as resolve,
        patch("app.services.google_sessions.add_initial_refresh_session") as insert,
    ):
        result = create_google_login_session(identity, session, auth_settings)
    resolve.assert_called_once_with(identity, session)
    assert result.user.id == "101" and result.refresh.token_type == "refresh"
    insert.assert_called_once_with(
        session,
        101,
        create_refresh_digest(result.refresh.token, auth_settings),
        result.refresh.issued_at,
        result.refresh.expires_at,
    )
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()
    assert not hasattr(result, "access")
    assert result.refresh.token not in repr(result) and identity.email not in repr(result)
    assert identity.subject not in repr(result)


@pytest.mark.parametrize(
    "target,stage",
    [
        ("issue_refresh_token", "token_issue"),
        ("add_initial_refresh_session", "refresh_session"),
        ("commit", "commit"),
    ],
)
def test_failure_rolls_back_and_never_returns_credentials(identity, account, auth_settings, target, stage):
    session = MagicMock(spec=Session)
    failure = (
        TokenIssueError("fixture-private")
        if target == "issue_refresh_token"
        else OperationalError("fixture-private-SQL", None, RuntimeError("fixture-private"))
    )
    owner = session if target == "commit" else "app.services.google_sessions"
    failure_patch = (
        patch.object(owner, target, side_effect=failure)
        if target == "commit"
        else patch(f"{owner}.{target}", side_effect=failure)
    )
    with (
        patch("app.services.google_sessions.get_or_create_google_user", return_value=account),
        patch("app.services.google_sessions.add_initial_refresh_session"),
        failure_patch,
    ):
        with pytest.raises(LoginUnavailable) as caught:
            create_google_login_session(identity, session, auth_settings)
    session.rollback.assert_called_once_with()
    if target != "commit":
        session.commit.assert_not_called()
    assert caught.value.failure.stage == stage
    assert "fixture-private" not in str(caught.value)


def test_account_failure_retains_original_failure_stage(identity, auth_settings):
    session = MagicMock(spec=Session)
    failure = FailureDetails(stage="google_identity_insert", error_type="IntegrityError")
    with patch(
        "app.services.google_sessions.get_or_create_google_user", side_effect=GoogleAccountUnavailable(failure=failure)
    ):
        with pytest.raises(LoginUnavailable) as caught:
            create_google_login_session(identity, session, auth_settings)
    assert caught.value.failure is failure
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_rollback_failure_is_safe(identity, auth_settings):
    session = MagicMock(spec=Session)
    session.rollback.side_effect = OperationalError("fixture-private", None, RuntimeError("fixture-private"))
    with patch("app.services.google_sessions.get_or_create_google_user", side_effect=RuntimeError("original")):
        with pytest.raises(LoginUnavailable) as caught:
            create_google_login_session(identity, session, auth_settings)
    assert caught.value.failure.stage == "rollback"
    assert "fixture-private" not in str(caught.value)


def test_programming_error_rolls_back_without_being_hidden(identity, account, auth_settings):
    session = MagicMock(spec=Session)
    with (
        patch("app.services.google_sessions.get_or_create_google_user", return_value=account),
        patch("app.services.google_sessions.add_initial_refresh_session", side_effect=RuntimeError("programming")),
    ):
        with pytest.raises(RuntimeError, match="programming"):
            create_google_login_session(identity, session, auth_settings)
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_cancellation_uses_the_same_refresh_compensation_as_local_login(identity, account, auth_settings):
    session = MagicMock(spec=Session)
    with (
        patch("app.services.google_sessions.get_or_create_google_user", return_value=account),
        patch("app.services.google_sessions.add_initial_refresh_session"),
    ):
        result = create_google_login_session(identity, session, auth_settings)
    with patch("app.services.google_sessions.cancel_login_refresh_session") as cancel:
        cancel_google_login(result, session, auth_settings)
    cancel.assert_called_once_with(101, result.refresh, session, auth_settings)
