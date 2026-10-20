"""Member quota storage that the quota service calls.

`user_id` is always the ID the server verified. Nothing here commits.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models.quota import ChatQuota, QuotaReservation


def database_time(session: Session) -> datetime:
    """The database clock now, not the transaction start, so a wait for a lock is not counted as earlier."""
    return session.scalars(select(func.clock_timestamp())).one()


def lock_quota(session: Session, user_id: int) -> ChatQuota:
    """Create the member's quota row if needed and lock it until the transaction ends.

    Every admission of the member takes this lock first, so two of them never
    count the same remaining reservation.
    """
    session.execute(insert(ChatQuota).values(user_id=user_id).on_conflict_do_nothing(index_elements=["user_id"]))
    statement = (
        select(ChatQuota)
        .where(ChatQuota.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalars(statement).one()


@dataclass(frozen=True, slots=True)
class Usage:
    """Settled and still reserved generations of one period; refunded ones no longer count."""

    used: int
    reserved: int


def _usage(session: Session, user_id: int, period_column: InstrumentedAttribute[datetime], start: datetime) -> Usage:
    statement = (
        select(QuotaReservation.state, func.count())
        .where(QuotaReservation.user_id == user_id, period_column == start, QuotaReservation.state != "refunded")
        .group_by(QuotaReservation.state)
    )
    counts: dict[str, int] = {state: count for state, count in session.execute(statement).tuples()}
    return Usage(used=counts.get("settled", 0), reserved=counts.get("reserved", 0))


def day_usage(session: Session, user_id: int, day_start: datetime) -> Usage:
    return _usage(session, user_id, QuotaReservation.day_start, day_start)


def six_hour_usage(session: Session, user_id: int, six_hour_start: datetime) -> Usage:
    return _usage(session, user_id, QuotaReservation.six_hour_start, six_hour_start)


def last_reserved_at(session: Session, user_id: int) -> datetime | None:
    """When the member last reserved, read without a lock; None before the first reservation."""
    return session.scalar(select(ChatQuota.last_reserved_at).where(ChatQuota.user_id == user_id))


def add_reservation(
    session: Session, quota: ChatQuota, *, day_start: datetime, six_hour_start: datetime, created_at: datetime
) -> QuotaReservation:
    """Add a reservation in the given periods; the caller holds `lock_quota` and has checked the limits."""
    reservation = QuotaReservation(
        user_id=quota.user_id,
        day_start=day_start,
        six_hour_start=six_hour_start,
        state="reserved",
        created_at=created_at,
    )
    session.add(reservation)
    quota.last_reserved_at = created_at
    session.flush()
    return reservation


def resolve_reservation(session: Session, reservation_id: int, *, state: str, refund_reason: str | None) -> bool:
    """Settle or refund the reservation only while it is still reserved; False when it was already resolved.

    A second resolution of the same reservation waits for the first one's row lock
    and then finds it no longer reserved, so each reservation is resolved once.
    """
    statement = (
        update(QuotaReservation)
        .where(QuotaReservation.id == reservation_id, QuotaReservation.state == "reserved")
        .values(state=state, refund_reason=refund_reason, resolved_at=func.clock_timestamp())
        .returning(QuotaReservation.id)
        .execution_options(synchronize_session="fetch")
    )
    return session.scalars(statement).one_or_none() is not None
