"""Lineage isolation, bounded recursive SQL, and rollback in guarded PostgreSQL."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from app.models.refresh_session import RefreshSession
from app.repositories.refresh_sessions import (
    CorruptRefreshLineage,
    add_initial_refresh_session,
    add_rotated_refresh_session,
    load_refresh_lineage,
    revoke_refresh_lineage,
    revoke_refresh_session,
)
from app.repositories.users import add_local_user, find_user_for_update
from app.services.refresh_sessions import lock_refresh_lineage

pytestmark = pytest.mark.db
NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def root(session, user_id):
    row = add_initial_refresh_session(session, user_id, uuid4().bytes * 2, NOW, NOW + timedelta(days=14))
    session.flush()
    return row


def successor(session, previous):
    row = add_rotated_refresh_session(session, previous, uuid4().bytes * 2, previous.issued_at + timedelta(seconds=1))
    session.flush()
    return row


@pytest.fixture
def graph(committed_signup_sessions):
    factory = committed_signup_sessions
    with factory() as session:
        a = add_local_user(session, f"{uuid4().hex}@example.test", "fixture-hash")
        b = add_local_user(session, f"{uuid4().hex}@example.test", "fixture-hash")
        first = root(session, a.id)
        second = successor(session, first)
        third = successor(session, second)
        other_login = root(session, a.id)
        other_user = root(session, b.id)
        session.commit()
        yield factory, a.id, b.id, (first.id, second.id, third.id), first.token_digest, other_login.id, other_user.id


@pytest.mark.parametrize("position", [0, 1, 2])
def test_any_session_resolves_exact_same_login_lineage(graph, position):
    factory, user_id, _, ids, _, _, _ = graph
    with factory() as session:
        find_user_for_update(session, user_id)
        lineage = load_refresh_lineage(session, user_id, ids[position])
        assert tuple(row.id for row in lineage) == ids
        assert all(row.user_id == user_id and row.expires_at == NOW + timedelta(days=14) for row in lineage)


def test_lock_helper_finds_user_digest_and_leaves_transaction_open(graph):
    factory, user_id, other_id, ids, digest, _, _ = graph
    with factory() as session:
        assert lock_refresh_lineage(session, other_id, digest) is None
        session.rollback()
        assert lock_refresh_lineage(session, user_id, b"x" * 32) is None
        session.rollback()
        assert lock_refresh_lineage(session, 9223372036854775807, digest) is None
        session.rollback()
        locked = lock_refresh_lineage(session, user_id, digest)
        assert locked is not None and locked.current.id == ids[0]
        assert tuple(row.id for row in locked.sessions) == ids
        assert session.in_transaction()


def test_revoke_preserves_previous_timestamp_other_logins_and_other_users(graph):
    factory, user_id, _, ids, digest, other_login, other_user = graph
    first_revocation = NOW + timedelta(seconds=1)
    later = NOW + timedelta(seconds=3)
    with factory() as session:
        locked = lock_refresh_lineage(session, user_id, digest)
        revoke_refresh_session(locked.current, first_revocation)
        revoke_refresh_lineage(locked.sessions, later)
        session.commit()
    with factory() as session:
        assert session.get(RefreshSession, ids[0]).revoked_at == first_revocation
        assert all(session.get(RefreshSession, row_id).revoked_at == later for row_id in ids[1:])
        assert session.get(RefreshSession, other_login).revoked_at is None
        assert session.get(RefreshSession, other_user).revoked_at is None


def test_insert_and_revocation_can_be_rolled_back_together(graph):
    factory, user_id, _, ids, digest, _, _ = graph
    with factory() as session:
        locked = lock_refresh_lineage(session, user_id, digest)
        new = successor(session, locked.sessions[-1])
        new_id = new.id
        revoke_refresh_lineage(locked.sessions, NOW + timedelta(seconds=4))
        session.flush()
        session.rollback()
    with factory() as session:
        assert session.get(RefreshSession, new_id) is None
        assert all(session.get(RefreshSession, row_id).revoked_at is None for row_id in ids)


def test_cycle_is_rejected_without_modifying_sessions(graph):
    factory, user_id, _, ids, _, _, _ = graph
    with factory() as session:
        find_user_for_update(session, user_id)
        session.execute(update(RefreshSession).where(RefreshSession.id == ids[0]).values(rotated_from_id=ids[-1]))
        with pytest.raises(CorruptRefreshLineage):
            load_refresh_lineage(session, user_id, ids[1])
        assert not session.dirty
        session.rollback()


def test_user_mismatch_and_missing_session_are_rejected(graph):
    factory, _, other_id, ids, _, _, _ = graph
    with factory() as session:
        find_user_for_update(session, other_id)
        for current_id in (ids[0], 9223372036854775807):
            with pytest.raises(CorruptRefreshLineage):
                load_refresh_lineage(session, other_id, current_id)


def test_changed_absolute_expiry_is_corrupt(graph):
    factory, user_id, _, ids, _, _, _ = graph
    with factory() as session:
        find_user_for_update(session, user_id)
        session.execute(
            update(RefreshSession).where(RefreshSession.id == ids[-1]).values(expires_at=NOW + timedelta(days=15))
        )
        with pytest.raises(CorruptRefreshLineage):
            load_refresh_lineage(session, user_id, ids[0])
        session.rollback()


@pytest.mark.parametrize("length", [4095, 4096, 4097])
def test_real_query_limit_is_independent_of_issuance_limit(committed_signup_sessions, length):
    factory = committed_signup_sessions
    with factory() as session:
        user = add_local_user(session, f"{uuid4().hex}@example.test", "fixture-hash")
        # 경계 데이터는 일괄 저장하고, 후임 삽입 동작은 작은 시나리오에서 별도 검증한다.
        ids = list(
            session.scalars(
                insert(RefreshSession).returning(RefreshSession.id, sort_by_parameter_order=True),
                [
                    {
                        "user_id": user.id,
                        "token_digest": uuid4().bytes * 2,
                        "issued_at": NOW + timedelta(seconds=index),
                        "expires_at": NOW + timedelta(days=14),
                    }
                    for index in range(length)
                ],
            )
        )
        session.execute(
            update(RefreshSession),
            [{"id": ids[index], "rotated_from_id": ids[index - 1]} for index in range(1, length)],
        )
        find_user_for_update(session, user.id)
        if length > 4096:
            for current_id in (ids[0], ids[-1]):
                with pytest.raises(CorruptRefreshLineage):
                    load_refresh_lineage(session, user.id, current_id)
        else:
            for current_id in (ids[0], ids[-1]):
                lineage = load_refresh_lineage(session, user.id, current_id)
                assert len(lineage) == length
                assert lineage[0].id == ids[0] and lineage[-1].id == ids[-1]
        session.rollback()
