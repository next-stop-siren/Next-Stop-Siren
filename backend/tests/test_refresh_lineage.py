"""Lock ordering and corruption paths that database constraints normally prevent."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models.refresh_session import RefreshSession
from app.repositories.refresh_sessions import CorruptRefreshLineage, load_refresh_lineage, revoke_refresh_lineage
from app.services.refresh_sessions import lock_refresh_lineage

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def row(identifier, parent=None, user_id=101):
    return RefreshSession(
        id=identifier,
        user_id=user_id,
        rotated_from_id=parent,
        token_digest=b"x" * 32,
        issued_at=NOW,
        expires_at=NOW + timedelta(days=14),
    )


@pytest.mark.parametrize("missing", [None, "user", "refresh"])
def test_lock_order_and_caller_transaction_ownership(missing):
    session = Mock(spec=Session)
    current = row(1)
    events = []

    def user_lock(*args):
        events.append("user")
        return None if missing == "user" else object()

    def refresh_lock(*args):
        events.append("refresh")
        return None if missing == "refresh" else current

    def lineage(*args):
        events.append("lineage")
        return (current,)

    with (
        patch("app.services.refresh_sessions.find_user_for_update", side_effect=user_lock),
        patch("app.services.refresh_sessions.find_refresh_session_for_update", side_effect=refresh_lock),
        patch("app.services.refresh_sessions.load_refresh_lineage", side_effect=lineage),
    ):
        result = lock_refresh_lineage(session, 101, b"x" * 32)
    if missing:
        assert result is None
        assert events == (["user"] if missing == "user" else ["user", "refresh"])
    else:
        assert result.current is current and result.sessions == (current,)
        assert events == ["user", "refresh", "lineage"]
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


@pytest.mark.parametrize("case", ["orphan", "foreign-parent", "foreign-child", "branch", "missing-current", "cycle"])
def test_corrupt_walk_never_revokes_or_commits(case):
    root, current = row(1), row(2, 1)
    walks = {
        "orphan": [[row(2, 999)]],
        "foreign-parent": [[current, row(1, user_id=202)]],
        "foreign-child": [[current, root], [root, row(2, 1, user_id=202)]],
        "branch": [[current, root], [root, current, row(3, 1)]],
        "missing-current": [[current, root], [root]],
        "cycle": [[current, root, current]],
    }[case]
    session = Mock(spec=Session)
    session.scalars.side_effect = walks
    with pytest.raises(CorruptRefreshLineage):
        load_refresh_lineage(session, 101, 2)
    assert root.revoked_at is None and current.revoked_at is None
    session.commit.assert_not_called()
    session.add.assert_not_called()


def test_naive_revocation_is_rejected_before_any_change():
    rows = (row(1), row(2, 1))
    with pytest.raises(ValueError):
        revoke_refresh_lineage(rows, NOW.replace(tzinfo=None))
    assert all(item.revoked_at is None for item in rows)


@pytest.mark.parametrize("cycle", [False, True])
def test_recursive_query_executes_in_memory(cycle):
    """실제 SQL 실행의 빠른 검사. PostgreSQL 잠금·제약 검사는 별도 db 테스트가 담당한다."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def sqlite_functions(connection, record):
        connection.create_function("octet_length", 1, len)

    try:
        RefreshSession.__table__.create(engine)
        with Session(engine) as session:
            rows = (row(1, 3 if cycle else None), row(2, 1), row(3, 2))
            for item in rows:
                item.token_digest = item.id.to_bytes(32, "big")
            session.add_all(rows)
            session.flush()
            if cycle:
                with pytest.raises(CorruptRefreshLineage):
                    load_refresh_lineage(session, 101, 2)
            else:
                assert tuple(item.id for item in load_refresh_lineage(session, 101, 2)) == (1, 2, 3)
    finally:
        engine.dispose()
