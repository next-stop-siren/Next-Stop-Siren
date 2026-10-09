"""Refresh token digests and rotation links within a single user's sessions."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="refresh_sessions_user_fk", ondelete="RESTRICT"), nullable=False
    )
    token_digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    rotated_from_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("token_digest", name="refresh_sessions_token_digest_uq"),
        UniqueConstraint("rotated_from_id", name="refresh_sessions_rotated_from_uq"),
        UniqueConstraint("id", "user_id", name="refresh_sessions_id_user_uq"),
        ForeignKeyConstraint(
            ["rotated_from_id", "user_id"],
            ["refresh_sessions.id", "refresh_sessions.user_id"],
            name="refresh_sessions_predecessor_fk",
            ondelete="RESTRICT",
        ),
        CheckConstraint("expires_at > issued_at", name="refresh_sessions_expiry_check"),
        CheckConstraint("octet_length(token_digest) = 32", name="refresh_sessions_digest_length_check"),
        Index("refresh_sessions_user_idx", user_id, expires_at.desc()),
    )
