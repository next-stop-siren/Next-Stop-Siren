"""Member quota reservation and settlement (#15, docs/04-reference/chat-policy.md).

A new question or manual retry reserves one generation in both the KST day and the
KST six-hour period when it is admitted. A completed answer whose storage is
confirmed settles the reservation; a failed, interrupted, canceled or expired one
refunds it. While the outcome is unknown the reservation stays reserved.

Settlement and refund belong to the periods the reservation was taken in, so a
refund after a reset does not raise the new period's remaining count. Resending
or reading the same request key must not call `reserve` again; the caller returns
the stored job instead, even with nothing left or before the interval has passed.

Nothing here commits: the caller admits the job and reserves in one transaction,
so neither succeeds alone. The member quota is not the provider's token quota or
cost, and a refund does not mean the provider did not charge.

Rejection reasons are the proposed error codes of the queue API draft
(`attempt_too_soon`, `quota_exceeded`); the queue checks such as `queue_full`
come before them and belong to the queue (#52).
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.models.quota import QuotaReservation
from app.repositories import quotas as repository
from app.repositories.quotas import Usage

KST = timezone(timedelta(hours=9), "KST")
DAILY_LIMIT = 400
SIX_HOUR_LIMIT = 100
MIN_INTERVAL_SECONDS = 10

RefundReason = Literal["failed", "interrupted", "canceled", "expired"]
# Ended before the provider was called.
_NOT_CALLED: frozenset[str] = frozenset({"canceled", "expired"})


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
    """Admission refused; `retry_after` is the whole seconds for the `Retry-After` header.

    For `quota_exceeded` it is the reset of every period with nothing left, the later
    one when both are empty. An earlier refund can free a reservation sooner, so it
    is the latest wait, not an exact one.
    """

    def __init__(self, reason: Literal["attempt_too_soon", "quota_exceeded"], retry_after: int) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class WindowLimit:
    """One period of the `limits` object: `remaining` = `limit` - `used` - `reserved`."""

    limit: int
    used: int
    reserved: int
    remaining: int
    resets_at: datetime


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    daily: WindowLimit
    six_hour: WindowLimit
    # Last admission + 10 seconds; None when a new generation may be admitted now.
    next_attempt_at: datetime | None


def _seconds_until(end: datetime, now: datetime) -> int:
    return max(1, math.ceil((end - now).total_seconds()))


def _window(limit: int, usage: Usage, resets_at: datetime) -> WindowLimit:
    remaining = max(0, limit - usage.used - usage.reserved)
    return WindowLimit(limit, usage.used, usage.reserved, remaining, resets_at)


def _limits(session: Session, user_id: int, now: datetime, last_reserved_at: datetime | None) -> QuotaLimits:
    periods = periods_at(now)
    allowed_at = None if last_reserved_at is None else last_reserved_at + timedelta(seconds=MIN_INTERVAL_SECONDS)
    return QuotaLimits(
        daily=_window(DAILY_LIMIT, repository.day_usage(session, user_id, periods.day_start), periods.day_end),
        six_hour=_window(
            SIX_HOUR_LIMIT,
            repository.six_hour_usage(session, user_id, periods.six_hour_start),
            periods.six_hour_end,
        ),
        next_attempt_at=allowed_at if allowed_at is not None and now < allowed_at else None,
    )


def reserve(session: Session, user_id: int, *, now: datetime | None = None) -> QuotaReservation:
    """Reserve one generation in both current periods, or raise `QuotaRejected`.

    The 10-second interval is checked before the limits. The member's quota row is
    locked first, so concurrent admissions from any tab, device or conversation are
    counted one after another. `now` replaces the database clock in tests.
    """
    quota = repository.lock_quota(session, user_id)
    if now is None:
        now = repository.database_time(session)
    limits = _limits(session, user_id, now, quota.last_reserved_at)
    if limits.next_attempt_at is not None:
        raise QuotaRejected("attempt_too_soon", _seconds_until(limits.next_attempt_at, now))
    empty = [window.resets_at for window in (limits.daily, limits.six_hour) if window.remaining == 0]
    if empty:
        raise QuotaRejected("quota_exceeded", _seconds_until(max(empty), now))
    periods = periods_at(now)
    return repository.add_reservation(
        session, quota, day_start=periods.day_start, six_hour_start=periods.six_hour_start, created_at=now
    )


def settle(session: Session, reservation_id: int) -> bool:
    """Count the reservation as used once its completed answer is stored; False if it was already resolved."""
    return repository.resolve_reservation(session, reservation_id, state="settled", refund_reason=None)


def refund(session: Session, reservation_id: int, reason: RefundReason) -> bool:
    """Give the reservation back to its own periods; False if it was already resolved."""
    return repository.resolve_reservation(session, reservation_id, state="refunded", refund_reason=reason)


def limits(session: Session, user_id: int, *, now: datetime | None = None) -> QuotaLimits:
    """The member's `limits` periods, read without locking; an admission may still be refused after it."""
    if now is None:
        now = repository.database_time(session)
    return _limits(session, user_id, now, repository.last_reserved_at(session, user_id))


def provider_may_have_charged(reservation: QuotaReservation) -> bool:
    """Whether the provider may have used tokens for this reservation, apart from what the member was charged.

    A settled one did. A failed, interrupted or still unresolved one may have,
    although the member is refunded or not yet charged. Only a canceled or expired
    one ended before any provider call.
    """
    return not (reservation.state == "refunded" and reservation.refund_reason in _NOT_CALLED)
