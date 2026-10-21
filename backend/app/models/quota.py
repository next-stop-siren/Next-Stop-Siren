"""Member quota tables for docs/04-reference/chat-policy.md (#15).

`chat_quotas` holds one row per member that admissions lock, so reservations of one
member are taken one at a time across tabs, devices and conversations. The
reservation rows are the ledger: a period's usage is the count of its reservations
that were not refunded, so there is no separate counter to drift from it.
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

RESERVATION_SHAPE = """
(state = 'reserved' AND refund_reason IS NULL AND resolved_at IS NULL)
OR (state = 'settled' AND refund_reason IS NULL AND resolved_at IS NOT NULL)
OR (state = 'refunded' AND refund_reason IS NOT NULL
    AND refund_reason IN ('failed', 'interrupted', 'canceled', 'expired') AND resolved_at IS NOT NULL)
"""


class ChatQuota(Base):
    __tablename__ = "chat_quotas"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="chat_quotas_user_fk", ondelete="RESTRICT"), primary_key=True
    )
    last_reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuotaReservation(Base):
    __tablename__ = "chat_quota_reservations"
    __table_args__ = (
        CheckConstraint("state IN ('reserved', 'settled', 'refunded')", name="chat_quota_reservations_state_ck"),
        CheckConstraint(RESERVATION_SHAPE, name="chat_quota_reservations_shape_ck"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_quotas.user_id", name="chat_quota_reservations_quota_fk", ondelete="RESTRICT")
    )
    # Start of the KST day and six-hour period the reservation was taken in.
    day_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    six_hour_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(Text)
    refund_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "chat_quota_reservations_day_idx",
    QuotaReservation.user_id,
    QuotaReservation.day_start,
    postgresql_where=text("state <> 'refunded'"),
)
Index(
    "chat_quota_reservations_six_hour_idx",
    QuotaReservation.user_id,
    QuotaReservation.six_hour_start,
    postgresql_where=text("state <> 'refunded'"),
)
