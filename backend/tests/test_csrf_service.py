"""CSRF read-only transaction boundaries and refresh response compensation guards."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.refresh_session import RefreshSession
from app.security.session_values import create_csrf_token
from app.services.csrf import read_csrf_token
from app.services.refresh import InvalidRefresh, RefreshUnavailable, cancel_refresh_rotation
from test_refresh_rotation import rotation as rotation


def test_csrf_read_releases_lock_without_commit_or_mutation(rotation, auth_settings):
    issued, locked = rotation
    session = MagicMock(spec=Session)
    with patch("app.services.csrf.lock_refresh_lineage", return_value=locked):
        assert read_csrf_token(issued.token, session, auth_settings) == create_csrf_token(issued.jti, auth_settings)
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
    session.add.assert_not_called()
    assert locked.current.revoked_at is None


@pytest.mark.parametrize("case", ["missing", "revoked", "expired", "metadata", "non-leaf", "active-parent"])
def test_csrf_read_requires_active_consistent_stored_session(rotation, auth_settings, case):
    issued, locked = rotation
    expected = InvalidRefresh
    if case == "revoked":
        locked.current.revoked_at = datetime.now(timezone.utc)
    elif case == "expired":
        locked.current.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif case == "metadata":
        locked.current.issued_at -= timedelta(seconds=1)
        expected = RefreshUnavailable
    elif case in {"non-leaf", "active-parent"}:
        other = RefreshSession(id=2, revoked_at=None)
        locked = replace(locked, sessions=(locked.current, other) if case == "non-leaf" else (other, locked.current))
        expected = RefreshUnavailable
    session = MagicMock(spec=Session)
    with patch("app.services.csrf.lock_refresh_lineage", return_value=None if case == "missing" else locked):
        with pytest.raises(expected):
            read_csrf_token(issued.token, session, auth_settings)
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


def test_csrf_bad_token_never_locks_database(auth_settings):
    session = MagicMock(spec=Session)
    with patch("app.services.csrf.lock_refresh_lineage") as lock:
        with pytest.raises(InvalidRefresh):
            read_csrf_token("invalid", session, auth_settings)
    lock.assert_not_called()
    session.rollback.assert_called_once()


@pytest.mark.parametrize("target", ["lookup", "rollback"])
def test_csrf_read_database_errors_have_safe_failure_stage(rotation, auth_settings, target):
    issued, locked = rotation
    session = MagicMock(spec=Session)
    error = OperationalError("private SQL", None, None)
    if target == "rollback":
        session.rollback.side_effect = error
    with patch(
        "app.services.csrf.lock_refresh_lineage",
        **({"side_effect": error} if target == "lookup" else {"return_value": locked}),
    ):
        with pytest.raises(RefreshUnavailable) as caught:
            read_csrf_token(issued.token, session, auth_settings)
    assert caught.value.failure.stage == ("user_lock" if target == "lookup" else "rollback")
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("case", ["missing", "metadata", "commit"])
def test_compensation_handles_missing_changed_and_failed_sessions(rotation, auth_settings, case):
    issued, locked = rotation
    session = MagicMock(spec=Session)
    if case == "metadata":
        locked.current.issued_at -= timedelta(seconds=1)
    elif case == "commit":
        session.commit.side_effect = OperationalError("private SQL", None, None)
    with patch("app.services.refresh.lock_refresh_lineage", return_value=None if case == "missing" else locked):
        if case == "missing":
            cancel_refresh_rotation(101, issued, session, auth_settings)
        else:
            with pytest.raises(RefreshUnavailable) as caught:
                cancel_refresh_rotation(101, issued, session, auth_settings)
            assert caught.value.failure.stage == "compensation"
    session.rollback.assert_called_once()
    if case != "commit":
        session.commit.assert_not_called()
