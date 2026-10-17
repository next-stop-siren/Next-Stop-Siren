"""Shared lock order for refresh, replay revocation, and logout services."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.refresh_session import RefreshSession
from app.repositories.refresh_sessions import find_refresh_session_for_update, load_refresh_lineage
from app.repositories.users import find_user_for_update


@dataclass(frozen=True, slots=True)
class LockedRefreshLineage:
    """현재 트랜잭션의 사용자 잠금 아래 읽은 현재 세션과 루트부터의 전체 계보."""

    current: RefreshSession
    sessions: tuple[RefreshSession, ...]


def lock_refresh_lineage(session: Session, user_id: int, token_digest: bytes) -> LockedRefreshLineage | None:
    """JWT·Origin·CSRF 검사 후 호출한다. 사용자 → 현재 세션 순서로 잠근다.

    같은 트랜잭션 안에서 회전·폐기를 마치고 호출자가 커밋 또는 롤백해야 한다.
    None은 사용자·digest가 없다는 뜻이며, 폐기·만료·발급 한도 판정은 후속 서비스가 맡는다.
    SQLAlchemy 오류와 CorruptRefreshLineage는 호출자에게 전달하며 여기서는 상태를 변경하지 않는다.
    """
    if find_user_for_update(session, user_id) is None:
        return None
    current = find_refresh_session_for_update(session, user_id, token_digest)
    if current is None:
        return None
    lineage = load_refresh_lineage(session, user_id, current.id)
    return LockedRefreshLineage(current=current, sessions=lineage)
