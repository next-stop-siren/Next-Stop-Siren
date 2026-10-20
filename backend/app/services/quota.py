"""Member quota reservation and settlement (#15, docs/04-reference/chat-policy.md).

A new question or manual retry reserves one generation in both the KST day and the
KST six-hour period when it is admitted. A completed answer whose storage is
confirmed settles the reservation; a failed, interrupted, cancelled or expired one
refunds it. While the outcome is unknown the reservation stays reserved.

Settlement and refund belong to the periods the reservation was taken in, so a
refund after a reset does not raise the new period's remaining count. Resending
or reading the same request key must not call `reserve` again; the caller returns
the stored job instead, even with nothing left.

Nothing here commits: the caller admits the job and reserves in one transaction,
so neither succeeds alone. The member quota is not the provider's token quota or
cost, and a refund does not mean the provider did not charge.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.models.quota import QuotaReservation
from app.repositories import quotas as repository

KST = timezone(timedelta(hours=9), "KST")
DAILY_LIMIT = 400
SIX_HOUR_LIMIT = 100
MIN_INTERVAL_SECONDS = 10

RefundReason = Literal["failed", "interrupted", "cancelled", "expired"]
# Ended before the provider was called.
_NOT_CALLED: frozenset[str] = frozenset({"cancelled", "expired"})


@dataclass(frozen=True, slots=True)
class Periods:
    """The KST day and six-hour period that contain a moment, as aware UTC-comparable times."""

    day_start: datetime
    day_end: datetime
    six_hour_start: datetime
    six_hour_end: datetime


def periods_at(moment: datetime) -> Periods:
    """Days reset at KST 00:00 and six-hour periods at KST 00:00, 06:00, 12:00 and 18:00."""
    local = moment.astimezone(KST)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    six_hour_start = day_start.replace(hour=local.hour - local.hour % 6)
    return Periods(day_start, day_start + timedelta(days=1), six_hour_start, six_hour_start + timedelta(hours=6))


class QuotaRejected(Exception):
    """Admission refused; `retry_after` is whole seconds until it can next be allowed.

    For a limit this is the period's reset. An earlier refund can free a reservation
    sooner, so it is the latest wait, not an exact one.
    """

    def __init__(self, reason: Literal["daily_limit", "six_hour_limit", "too_soon"], retry_after: int) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    daily_remaining: int
    six_hour_remaining: int
    daily_resets_at: datetime
    six_hour_resets_at: datetime
    # None when a new generation may be reserved now as far as the interval goes.
    next_attempt_at: datetime | None


def _seconds_until(end: datetime, now: datetime) -> int:
    return max(1, math.ceil((end - now).total_seconds()))


def reserve(session: Session, user_id: int, *, now: datetime | None = None) -> QuotaReservation:
    """Reserve one generation in both current periods, or raise `QuotaRejected`.

    The member's quota row is locked first, so concurrent admissions from any tab,
    device or conversation are counted one after another. `now` replaces the
    database clock in tests.
    """
    quota = repository.lock_quota(session, user_id)
    if now is None:
        now = repository.database_time(session)
    periods = periods_at(now)
    if repository.count_day(session, user_id, periods.day_start) >= DAILY_LIMIT:
        raise QuotaRejected("daily_limit", _seconds_until(periods.day_end, now))
    if repository.count_six_hours(session, user_id, periods.six_hour_start) >= SIX_HOUR_LIMIT:
        raise QuotaRejected("six_hour_limit", _seconds_until(periods.six_hour_end, now))
    if quota.last_reserved_at is not None:
        allowed_at = quota.last_reserved_at + timedelta(seconds=MIN_INTERVAL_SECONDS)
        if now < allowed_at:
            raise QuotaRejected("too_soon", _seconds_until(allowed_at, now))
    return repository.add_reservation(
        session, quota, day_start=periods.day_start, six_hour_start=periods.six_hour_start, created_at=now
    )


def settle(session: Session, reservation_id: int) -> bool:
    """Count the reservation as used once its completed answer is stored; False if it was already resolved."""
    return repository.resolve_reservation(session, reservation_id, state="settled", refund_reason=None)


def refund(session: Session, reservation_id: int, reason: RefundReason) -> bool:
    """Give the reservation back to its own periods; False if it was already resolved."""
    return repository.resolve_reservation(session, reservation_id, state="refunded", refund_reason=reason)


def status(session: Session, user_id: int, *, now: datetime | None = None) -> QuotaStatus:
    """What the member has left, read without locking; an admission may still be refused after it."""
    if now is None:
        now = repository.database_time(session)
    periods = periods_at(now)
    last = repository.last_reserved_at(session, user_id)
    allowed_at = None if last is None else last + timedelta(seconds=MIN_INTERVAL_SECONDS)
    return QuotaStatus(
        daily_remaining=max(0, DAILY_LIMIT - repository.count_day(session, user_id, periods.day_start)),
        six_hour_remaining=max(
            0, SIX_HOUR_LIMIT - repository.count_six_hours(session, user_id, periods.six_hour_start)
        ),
        daily_resets_at=periods.day_end,
        six_hour_resets_at=periods.six_hour_end,
        next_attempt_at=allowed_at if allowed_at is not None and now < allowed_at else None,
    )


def provider_may_have_charged(reservation: QuotaReservation) -> bool:
    """Whether the provider may have used tokens for this reservation, apart from what the member was charged.

    A settled one did. A failed, interrupted or still unresolved one may have,
    although the member is refunded or not yet charged. Only a cancelled or expired
    one ended before any provider call.
    """
    return not (reservation.state == "refunded" and reservation.refund_reason in _NOT_CALLED)
