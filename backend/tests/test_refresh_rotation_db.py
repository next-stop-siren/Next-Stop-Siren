"""Real committed refresh rotations and atomic rollback in guarded PostgreSQL."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.refresh_session import RefreshSession
from app.repositories.refresh_sessions import add_initial_refresh_session
from app.repositories.users import add_google_user, add_local_user
from app.security.session_values import create_refresh_digest
from app.security.tokens import TokenIssueError, issue_refresh_token, verify_access_token, verify_refresh_token
from app.services.refresh import InvalidRefresh, RefreshUnavailable, rotate_refresh_session

pytestmark = pytest.mark.db


@pytest.fixture(params=["local", "google"])
def stored(committed_signup_sessions, auth_settings, request):
    factory = committed_signup_sessions
    with factory() as session:
        email = f"rotation-{uuid4().hex}@example.test"
        user = (
            add_local_user(session, email, "fixture-hash")
            if request.param == "local"
            else add_google_user(session, email)
        )
        other = add_local_user(session, f"other-{uuid4().hex}@example.test", "fixture-hash")
        issued_at = datetime.now(timezone.utc) - timedelta(days=7)
        refresh = issue_refresh_token(user.id, auth_settings, now=issued_at)
        initial = add_initial_refresh_session(
            session, user.id, create_refresh_digest(refresh.token, auth_settings), refresh.issued_at, refresh.expires_at
        )
        others = []
        for user_id in (user.id, other.id):
            token = issue_refresh_token(user_id, auth_settings)
            others.append(
                add_initial_refresh_session(
                    session,
                    user_id,
                    create_refresh_digest(token.token, auth_settings),
                    token.issued_at,
                    token.expires_at,
                )
            )
        session.commit()
        yield factory, user.id, refresh, initial.id, tuple(row.id for row in others)


def snapshot(stored):
    factory = stored[0]
    with factory() as session:
        return tuple(
            (row.id, row.user_id, row.token_digest, row.rotated_from_id, row.issued_at, row.expires_at, row.revoked_at)
            for row in session.scalars(select(RefreshSession).order_by(RefreshSession.id))
        )


def test_repeated_rotation_preserves_absolute_expiry_and_independent_logins(stored, auth_settings):
    factory, user_id, original, original_id, other_ids = stored
    token = original.token
    previous_id = original_id
    jtis = {original.jti}
    for _ in range(2):
        with factory() as session:
            result = rotate_refresh_session(token, session, auth_settings)
            assert not session.in_transaction()
        # 별도 연결에서 읽어 커밋된 결과인지 확인한다.
        with factory() as reader:
            previous = reader.get(RefreshSession, previous_id)
            children = list(reader.scalars(select(RefreshSession).where(RefreshSession.rotated_from_id == previous_id)))
            assert previous.revoked_at is not None and len(children) == 1
            child = children[0]
            assert child.user_id == user_id and child.revoked_at is None
            assert child.token_digest == create_refresh_digest(result.refresh.token, auth_settings)
            assert len(child.token_digest) == 32
            assert child.expires_at == original.expires_at == result.refresh.expires_at
            assert child.issued_at == result.refresh.issued_at
            assert all(reader.get(RefreshSession, identifier).revoked_at is None for identifier in other_ids)
            previous_id = child.id
        assert verify_access_token(result.access.token, auth_settings) == user_id
        assert verify_refresh_token(result.refresh.token, auth_settings).user_id == user_id
        assert result.refresh.jti not in jtis
        jtis.add(result.refresh.jti)
        token = result.refresh.token
    with factory() as session:
        active = list(
            session.scalars(
                select(RefreshSession).where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None))
            )
        )
        assert {row.id for row in active} == {previous_id, other_ids[0]}


@pytest.mark.parametrize("target", ["commit", "insert", "access", "refresh"])
def test_failure_rolls_back_predecessor_and_successor(stored, auth_settings, target):
    factory, _, original, _, _ = stored
    before = snapshot(stored)

    def failed_commit(session):
        # UPDATE와 INSERT를 DB에 보낸 뒤 실패시켜 롤백을 검증한다.
        session.flush()
        raise OperationalError("private SQL", None, RuntimeError("private failure"))

    def failed_insert(*args):
        args[0].flush()
        raise OperationalError("private SQL", None, RuntimeError("private failure"))

    failure = {
        "commit": patch.object(Session, "commit", failed_commit),
        "insert": patch("app.services.refresh.add_rotated_refresh_session", side_effect=failed_insert),
        "access": patch("app.services.refresh.issue_access_token", side_effect=TokenIssueError("private key")),
        "refresh": patch("app.services.refresh.issue_refresh_token", side_effect=TokenIssueError("private key")),
    }[target]
    with factory() as session, failure:
        with pytest.raises(RefreshUnavailable) as caught:
            rotate_refresh_session(original.token, session, auth_settings)
        assert "private" not in str(caught.value)
        assert not session.in_transaction()
    assert snapshot(stored) == before
    # 실패 후 같은 refresh로 다시 시도할 수 있다.
    with factory() as session:
        assert rotate_refresh_session(original.token, session, auth_settings).refresh.expires_at == original.expires_at


def test_revoked_refresh_does_not_create_another_successor(stored, auth_settings):
    factory, _, original, _, _ = stored
    with factory() as session:
        rotate_refresh_session(original.token, session, auth_settings)
    before = snapshot(stored)
    with factory() as session:
        with pytest.raises(InvalidRefresh):
            rotate_refresh_session(original.token, session, auth_settings)
    assert snapshot(stored) == before


def test_valid_signature_without_stored_digest_is_rejected(stored, auth_settings):
    factory, user_id, _, _, _ = stored
    before = snapshot(stored)
    unstored = issue_refresh_token(user_id, auth_settings)
    with factory() as session:
        with pytest.raises(InvalidRefresh):
            rotate_refresh_session(unstored.token, session, auth_settings)
    assert snapshot(stored) == before
