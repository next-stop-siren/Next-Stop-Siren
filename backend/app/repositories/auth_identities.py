"""Google identity persistence keyed exclusively by verified issuer and subject."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth_identity import AuthIdentity


def find_google_identity(session: Session, issuer: str, subject: str) -> AuthIdentity | None:
    """Look up the verified issuer and subject, refreshing any existing ORM object."""
    return session.scalar(
        select(AuthIdentity)
        .where(AuthIdentity.provider == "google", AuthIdentity.issuer == issuer, AuthIdentity.subject == subject)
        .execution_options(populate_existing=True)
    )


def add_google_identity(session: Session, user_id: int, issuer: str, subject: str) -> AuthIdentity:
    """Link a user to a Google identity; the caller owns the commit."""
    identity = AuthIdentity(user_id=user_id, provider="google", issuer=issuer, subject=subject)
    session.add(identity)
    session.flush()
    return identity
