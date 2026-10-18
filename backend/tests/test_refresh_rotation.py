"""Normal rotation guards, failure boundaries, and safe errors without a DB."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.refresh_session import RefreshSession
from app.security.session_values import create_refresh_digest
from app.security.tokens import TokenIssueError, issue_refresh_token
from app.services.refresh import (
    InvalidRefresh,
    RefreshLimitReached,
    RefreshRace,
    RefreshReuseDetected,
    RefreshUnavailable,
    rotate_refresh_session,
)
from app.services.refresh_sessions import LockedRefreshLineage

MODULE = "app.services.refresh"


@pytest.fixture
def rotation(auth_settings):
    issued = issue_refresh_token(101, auth_settings, now=datetime.now(timezone.utc) - timedelta(days=7))
    row = RefreshSession(
        id=1,
        user_id=101,
        token_digest=create_refresh_digest(issued.token, auth_settings),
        issued_at=issued.issued_at,
        expires_at=issued.expires_at,
        revoked_at=None,
        rotated_from_id=None,
    )
    return issued, LockedRefreshLineage(current=row, sessions=(row,))


def test_normal_rotation_stores_only_digest_and_returns_after_commit(rotation, auth_settings):
    issued, locked = rotation
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked) as lock:
        result = rotate_refresh_session(issued.token, session, auth_settings)
    lock.assert_called_once_with(session, 101, create_refresh_digest(issued.token, auth_settings))
    successor = session.add.call_args.args[0]
    assert locked.current.revoked_at is not None
    assert successor.user_id == 101 and successor.rotated_from_id == locked.current.id
    assert successor.token_digest == create_refresh_digest(result.refresh.token, auth_settings)
    assert len(successor.token_digest) == 32
    assert successor.expires_at == result.refresh.expires_at == issued.expires_at
    assert successor.revoked_at is None
    assert result.refresh.jti != issued.jti
    assert result.access.token not in repr(result) and result.refresh.token not in repr(result)
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "revoked", "expired-after-lock", "metadata", "non-leaf", "active-parent"])
def test_invalid_or_corrupt_state_never_commits(rotation, auth_settings, case):
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
            rotate_refresh_session(issued.token, session, auth_settings)
    session.commit.assert_not_called()
    session.add.assert_not_called()
    session.rollback.assert_called_once_with()


def test_limit_is_guarded_before_issuance(rotation, auth_settings):
    issued, locked = rotation
    parent = RefreshSession(id=2, revoked_at=datetime.now(timezone.utc))
    locked = replace(locked, sessions=(parent,) * 4094 + (locked.current,))
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked):
        with pytest.raises(RefreshLimitReached):
            rotate_refresh_session(issued.token, session, auth_settings)
    session.add.assert_not_called()
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()
    assert locked.current.revoked_at is not None


@pytest.mark.parametrize(
    "target,stage",
    [
        ("lock_refresh_lineage", "user_lock"),
        ("issue_access_token", "token_issue"),
        ("issue_refresh_token", "token_issue"),
        ("add_rotated_refresh_session", "refresh_session"),
        ("commit", "commit"),
    ],
)
def test_failures_roll_back_without_returning_credentials(rotation, auth_settings, target, stage):
    issued, locked = rotation
    session = MagicMock(spec=Session)
    error = (
        TokenIssueError("private-key") if target.startswith("issue_") else OperationalError("private SQL", None, None)
    )
    failure = (
        patch.object(session, "commit", side_effect=error)
        if target == "commit"
        else patch(f"{MODULE}.{target}", side_effect=error)
    )
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked), failure:
        with pytest.raises(RefreshUnavailable) as caught:
            rotate_refresh_session(issued.token, session, auth_settings)
    assert caught.value.failure.stage == stage
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    session.rollback.assert_called_once_with()
    if target != "commit":
        session.commit.assert_not_called()


def test_malformed_refresh_never_locks_database(auth_settings):
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage") as lock:
        with pytest.raises(InvalidRefresh):
            rotate_refresh_session("private-invalid-jwt", session, auth_settings)
    lock.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_called_once_with()


def test_expiry_while_waiting_for_lock_is_rejected(rotation, auth_settings):
    issued, locked = rotation
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked), patch(f"{MODULE}.datetime") as clock:
        clock.now.return_value = issued.expires_at
        with pytest.raises(InvalidRefresh):
            rotate_refresh_session(issued.token, session, auth_settings)
    session.add.assert_not_called()
    session.rollback.assert_called_once_with()


def test_rollback_failure_and_programming_errors(rotation, auth_settings):
    issued, _ = rotation
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", side_effect=RuntimeError("programming")):
        with pytest.raises(RuntimeError, match="programming"):
            rotate_refresh_session(issued.token, session, auth_settings)
    session.rollback.assert_called_once_with()
    session.rollback.side_effect = OperationalError("private SQL", None, None)
    with pytest.raises(RefreshUnavailable) as caught:
        rotate_refresh_session("invalid", session, auth_settings)
    assert caught.value.failure.stage == "rollback"


@pytest.mark.parametrize("elapsed", [0, 4.999999, 5, 5.000001, 6])
def test_replay_window_boundary_and_committed_revocation(rotation, auth_settings, elapsed):
    issued, locked = rotation
    now = datetime.now(timezone.utc)
    revoked_at = now - timedelta(seconds=elapsed)
    locked.current.revoked_at = revoked_at
    child = RefreshSession(
        id=2,
        user_id=101,
        rotated_from_id=1,
        issued_at=issued.issued_at,
        expires_at=issued.expires_at,
        revoked_at=None,
    )
    locked = replace(locked, sessions=(locked.current, child))
    session = MagicMock(spec=Session)
    expected = RefreshRace if elapsed <= 5 else RefreshReuseDetected
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked), patch(f"{MODULE}.datetime") as clock:
        clock.now.return_value = now
        with pytest.raises(expected) as caught:
            rotate_refresh_session(issued.token, session, auth_settings)
    assert locked.current.revoked_at == revoked_at
    session.add.assert_not_called()
    if elapsed <= 5:
        assert caught.value.retry_after == 1
        assert child.revoked_at is None
        session.commit.assert_not_called()
        session.rollback.assert_called_once_with()
    else:
        assert caught.value.clear_cookies is True
        assert child.revoked_at == now
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()


def test_fully_revoked_lineage_is_not_a_race(rotation, auth_settings):
    issued, locked = rotation
    now = datetime.now(timezone.utc)
    locked.current.revoked_at = now
    child = RefreshSession(id=2, revoked_at=now)
    locked = replace(locked, sessions=(locked.current, child))
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked):
        with pytest.raises(InvalidRefresh) as caught:
            rotate_refresh_session(issued.token, session, auth_settings)
    assert caught.value.clear_cookies is False
    session.commit.assert_not_called()
    session.rollback.assert_called_once_with()


def test_future_revocation_is_corrupt_and_emits_safe_alert(rotation, auth_settings, caplog):
    issued, locked = rotation
    locked.current.revoked_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    locked = replace(locked, sessions=(locked.current, RefreshSession(id=2, revoked_at=None)))
    session = MagicMock(spec=Session)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked):
        with pytest.raises(RefreshUnavailable):
            rotate_refresh_session(issued.token, session, auth_settings)
    assert "event=refresh_lineage_corrupt" in caplog.text
    assert issued.token not in caplog.text
    session.commit.assert_not_called()


@pytest.mark.parametrize("decision", ["reuse", "limit"])
def test_revocation_commit_failure_never_reports_committed_rejection(rotation, auth_settings, decision):
    issued, locked = rotation
    now = datetime.now(timezone.utc)
    if decision == "reuse":
        locked.current.revoked_at = now - timedelta(seconds=6)
        locked = replace(locked, sessions=(locked.current, RefreshSession(id=2, revoked_at=None)))
    else:
        parent = RefreshSession(id=2, revoked_at=now - timedelta(seconds=1))
        locked = replace(locked, sessions=(parent,) * 4094 + (locked.current,))
    session = MagicMock(spec=Session)
    session.commit.side_effect = OperationalError("private SQL", None, None)
    with patch(f"{MODULE}.lock_refresh_lineage", return_value=locked):
        with pytest.raises(RefreshUnavailable) as caught:
            rotate_refresh_session(issued.token, session, auth_settings)
    assert caught.value.failure.stage == "commit"
    assert not hasattr(caught.value, "clear_cookies")
    session.add.assert_not_called()
    session.rollback.assert_called_once_with()
