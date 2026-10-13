"""Initial refresh session persistence; token plaintext is never accepted."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.refresh_session import RefreshSession


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
    """사용자·digest로 세션을 잠그고 최신 DB 값을 읽는다."""
    return session.scalar(
        select(RefreshSession)
        .where(RefreshSession.user_id == user_id, RefreshSession.token_digest == token_digest)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def delete_refresh_session(session: Session, refresh: RefreshSession) -> None:
    """사용자보다 세션을 먼저 삭제하도록 커밋 없이 flush한다."""
    session.delete(refresh)
    session.flush()
