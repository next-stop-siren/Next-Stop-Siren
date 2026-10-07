"""Common user records for separate local and Google accounts."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("email = btrim(email) AND length(email) BETWEEN 3 AND 320", name="users_email_check"),
        CheckConstraint("password_hash IS NULL OR length(password_hash) > 0", name="users_password_hash_check"),
        Index("users_local_email_uq", func.lower(email), unique=True, postgresql_where=password_hash.is_not(None)),
    )
