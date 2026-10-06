"""Common user table shared by authentication and chat ownership.

Constraint conditions follow docs/04-reference/database-reference.sql.
A local account and a Google account with the same email are separate rows;
only local accounts are kept unique by email.
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = btrim(email) AND length(email) BETWEEN 3 AND 320", name="users_email_ck"),
        CheckConstraint("password_hash IS NULL OR length(password_hash) > 0", name="users_password_hash_ck"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    # NULL for a Google-only account.
    password_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index(
    "users_local_email_uq",
    func.lower(User.email),
    unique=True,
    postgresql_where=text("password_hash IS NOT NULL"),
)
