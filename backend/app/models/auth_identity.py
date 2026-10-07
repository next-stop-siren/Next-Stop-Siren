"""Google identities keyed by verified issuer and subject."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuthIdentity(Base):
    __tablename__ = "auth_identities"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="auth_identities_user_fk", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="google")
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("provider = 'google'", name="auth_identities_provider_check"),
        CheckConstraint("length(issuer) > 0 AND length(subject) > 0", name="auth_identities_issuer_subject_check"),
        UniqueConstraint("issuer", "subject", name="auth_identities_issuer_subject_uq"),
        Index("auth_identities_user_idx", "user_id"),
    )
