"""Initial refresh session persistence; token plaintext is never accepted."""

from datetime import datetime

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
