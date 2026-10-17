"""Logout state checks, transaction ownership, and safe failure propagation."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.refresh_session import RefreshSession
from app.repositories.refresh_sessions import CorruptRefreshLineage
from app.security.session_values import create_refresh_digest
from app.security.tokens import issue_access_token, issue_refresh_token
from app.services.logout import logout_refresh_session
from app.services.refresh import InvalidRefresh, RefreshUnavailable
from test_refresh_rotation import rotation as rotation

MODULE = "app.services.logout"


def test_logout_revokes_and_commits_without_issuing_tokens(rotation, auth_settings):
    issued, locked = rotation
    old_revocation = datetime.now(timezone.utc) - timedelta(seconds=1)
    parent = RefreshSession(id=2, revoked_at=old_revocation)
    locked = replace(locked, sessions=(parent, locked.current))
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked) as lock:
        assert logout_refresh_session(issued.token, session, auth_settings) is None
    lock.assert_called_once_with(session, 101, create_refresh_digest(issued.token, auth_settings))
    assert locked.current.revoked_at is not None
    assert parent.revoked_at == old_revocation
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()
    session.add.assert_not_called()
    session.delete.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "revoked", "expired-after-lock", "metadata", "non-leaf", "active-parent"])
def test_invalid_state_never_commits(rotation, auth_settings, case, caplog):
    issued, locked = rotation
    expected = InvalidRefresh
    if case == "revoked":
        locked.current.revoked_at = datetime.now(timezone.utc)
    elif case == "expired-after-lock":
        locked.current.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif case == "metadata":
        locked.current.issued_at -= timedelta(seconds=1)
        expected = RefreshUnavailable
    elif case in {"non-leaf", "active-parent"}:
        other = RefreshSession(id=2, revoked_at=None)
        locked = replace(locked, sessions=(locked.current, other) if case == "non-leaf" else (other, locked.current))
        expected = RefreshUnavailable
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=None if case == "missing" else locked):
        with pytest.raises(expected):
            logout_refresh_session(issued.token, session, auth_settings)
    session.commit.assert_not_called()
    session.rollback.assert_called_once_with()
    if expected == RefreshUnavailable:
        assert "event=logout_lineage_corrupt" in caplog.text
        assert issued.token not in caplog.text


@pytest.mark.parametrize("kind", ["malformed", "access", "expired"])
def test_bad_refresh_is_rejected_before_lock(rotation, auth_settings, kind):
    issued, _ = rotation
    token = {
        "malformed": "private-invalid-jwt",
        "access": issue_access_token(101, auth_settings).token,
        "expired": issued.token,
    }[kind]
    session = MagicMock(spec=Session)
    if kind == "expired":
        token = issue_refresh_token(101, auth_settings, now=datetime.now(timezone.utc) - timedelta(days=15)).token
    with patch(f"{MODULE}.lock_refresh_lineage") as lock:
        with pytest.raises(InvalidRefresh):
            logout_refresh_session(token, session, auth_settings)
    lock.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_called_once_with()


def test_expiry_during_lock_wait_is_rejected(rotation, auth_settings):
    issued, locked = rotation
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked), patch(f"{MODULE}.datetime") as clock:
        clock.now.return_value = issued.expires_at
        with pytest.raises(InvalidRefresh):
            logout_refresh_session(issued.token, session, auth_settings)
    assert locked.current.revoked_at is None
    session.commit.assert_not_called()
    session.rollback.assert_called_once_with()


@pytest.mark.parametrize("target", ["lock", "revoke", "commit", "corrupt", "programming"])
def test_failures_roll_back_and_do_not_expose_details(rotation, auth_settings, target):
    issued, locked = rotation
    session = MagicMock(spec=Session)
    error = OperationalError("private SQL", None, None)
    failure = {
        "lock": patch(f"{MODULE}.lock_refresh_lineage", side_effect=error),
        "revoke": patch(f"{MODULE}.revoke_refresh_lineage", side_effect=error),
        "commit": patch.object(session, "commit", side_effect=error),
        "corrupt": patch(f"{MODULE}.lock_refresh_lineage", side_effect=CorruptRefreshLineage()),
        "programming": patch(f"{MODULE}.lock_refresh_lineage", side_effect=RuntimeError("programming")),
    }[target]
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked), failure:
        with pytest.raises(RuntimeError if target == "programming" else RefreshUnavailable) as caught:
            logout_refresh_session(issued.token, session, auth_settings)
    session.rollback.assert_called_once_with()
    if target != "commit":
        session.commit.assert_not_called()
    assert "private" not in str(caught.value)


def test_rollback_failure_is_safe(rotation, auth_settings):
    issued, _ = rotation
    session = MagicMock(spec=Session)
    session.rollback.side_effect = OperationalError("private SQL", None, None)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=None):
        with pytest.raises(RefreshUnavailable) as caught:
            logout_refresh_session(issued.token, session, auth_settings)
    assert caught.value.failure.stage == "rollback"
    assert "private" not in str(caught.value)
