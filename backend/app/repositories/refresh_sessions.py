"""Refresh persistence and bounded lineage traversal; callers own the transaction."""

from datetime import datetime

from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from app.models.refresh_session import RefreshSession

MAX_REFRESH_LINEAGE_ISSUANCES = 4095
MAX_REFRESH_LINEAGE_QUERY = 4096


class CorruptRefreshLineage(Exception):
    """순환·사용자 불일치·연결 손상·조회 상한 초과. HTTP 계층에서 503과 경보로 처리한다."""

    def __init__(self) -> None:
        super().__init__("refresh 계보를 안전하게 확인할 수 없습니다.")


def add_initial_refresh_session(
    session: Session,
    user_id: int,
    token_digest: bytes,
    issued_at: datetime,
    expires_at: datetime,
) -> RefreshSession:
    refresh = RefreshSession(
        user_id=user_id,
        token_digest=token_digest,
        issued_at=issued_at,
        expires_at=expires_at,
        rotated_from_id=None,
        revoked_at=None,
    )
    session.add(refresh)
    return refresh


def find_refresh_session_for_update(session: Session, user_id: int, token_digest: bytes) -> RefreshSession | None:
    """호출자가 사용자 행을 먼저 잠근 뒤, 사용자·digest로 세션의 최신 값을 잠가 읽는다."""
    return session.scalar(
        select(RefreshSession)
        .where(RefreshSession.user_id == user_id, RefreshSession.token_digest == token_digest)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _walk_lineage(session: Session, user_id: int, start_id: int, *, ancestors: bool) -> list[RefreshSession]:
    """재귀 CTE로 왕복 쿼리를 줄이며, 상한 초과를 확인할 한 행까지만 읽는다."""
    model = RefreshSession
    walk = select(model.id, model.user_id, model.rotated_from_id, literal(1).label("depth")).where(model.id == start_id)
    chain = walk.cte("refresh_chain", recursive=True)
    link = model.id == chain.c.rotated_from_id if ancestors else model.rotated_from_id == chain.c.id
    chain = chain.union_all(
        select(model.id, model.user_id, model.rotated_from_id, (chain.c.depth + 1).label("depth"))
        .join(chain, link)
        .where(chain.c.depth <= MAX_REFRESH_LINEAGE_QUERY, chain.c.user_id == user_id)
    )
    rows = list(
        session.scalars(
            select(model)
            .join(chain, model.id == chain.c.id)
            .order_by(chain.c.depth, model.id)
            .limit(MAX_REFRESH_LINEAGE_QUERY + 1)
            .execution_options(populate_existing=True)
        )
    )
    if (
        not rows
        or len(rows) > MAX_REFRESH_LINEAGE_QUERY
        or len({row.id for row in rows}) != len(rows)
        or any(row.user_id != user_id for row in rows)
    ):
        raise CorruptRefreshLineage()
    return rows


def load_refresh_lineage(session: Session, user_id: int, current_id: int) -> tuple[RefreshSession, ...]:
    """사용자 잠금 안에서 현재 세션의 루트와 전체 계보를 확인한다. 커밋·폐기하지 않는다."""
    ancestors = _walk_lineage(session, user_id, current_id, ancestors=True)
    root = ancestors[-1]
    if root.rotated_from_id is not None:
        raise CorruptRefreshLineage()
    rows = _walk_lineage(session, user_id, root.id, ancestors=False)
    if current_id not in {row.id for row in rows}:
        raise CorruptRefreshLineage()
    for index, row in enumerate(rows):
        expected_parent = rows[index - 1].id if index else None
        if row.rotated_from_id != expected_parent or row.expires_at != root.expires_at:
            raise CorruptRefreshLineage()
    return tuple(rows)


def add_rotated_refresh_session(
    session: Session,
    predecessor: RefreshSession,
    token_digest: bytes,
    issued_at: datetime,
) -> RefreshSession:
    """잠금으로 확인한 전임의 사용자·절대 만료를 상속한다. 발급 한도와 폐기는 서비스 책임이다."""
    refresh = RefreshSession(
        user_id=predecessor.user_id,
        token_digest=token_digest,
        rotated_from_id=predecessor.id,
        issued_at=issued_at,
        expires_at=predecessor.expires_at,
        revoked_at=None,
    )
    session.add(refresh)
    return refresh


def revoke_refresh_session(refresh: RefreshSession, revoked_at: datetime) -> None:
    """폐기 이력을 지우거나 덮어쓰지 않는다. 기존 객체는 호출자의 세션에서 추적된다."""
    if revoked_at.utcoffset() is None:
        raise ValueError("시간대가 있는 폐기 시각이 필요합니다.")
    if refresh.revoked_at is None:
        refresh.revoked_at = revoked_at


def revoke_refresh_lineage(lineage: tuple[RefreshSession, ...], revoked_at: datetime) -> None:
    """잠금 아래 조회·검증된 계보의 활성 세션만 폐기한다. 커밋은 호출자가 수행한다."""
    if revoked_at.utcoffset() is None:
        raise ValueError("시간대가 있는 폐기 시각이 필요합니다.")
    for refresh in lineage:
        revoke_refresh_session(refresh, revoked_at)


def delete_refresh_session(session: Session, refresh: RefreshSession) -> None:
    """사용자보다 세션을 먼저 삭제하도록 커밋 없이 flush한다."""
    session.delete(refresh)
    session.flush()
