"""Unit checks for member quota periods, admission order, `limits` and provider charge reporting."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.quota import QuotaReservation
from app.repositories.quotas import Usage
from app.services import quota
from app.services.quota import KST, QuotaRejected, periods_at, provider_may_have_charged


def kst(hour: int, minute: int = 0, second: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=KST)


@pytest.mark.parametrize(
    ("moment", "six_hour_start"),
    [
        (kst(0), kst(0)),
        (kst(5, 59, 59), kst(0)),
        (kst(6), kst(6)),
        (kst(11, 30), kst(6)),
        (kst(12), kst(12)),
        (kst(18), kst(18)),
        (kst(23, 59, 59), kst(18)),
    ],
)
def test_six_hour_periods_reset_at_kst_0_6_12_18(moment, six_hour_start):
    periods = periods_at(moment)
    assert periods.six_hour_start == six_hour_start
    assert periods.six_hour_end == six_hour_start + timedelta(hours=6)
    assert periods.day_start == kst(0)
    assert periods.day_end == kst(0, day=21)


def test_the_kst_day_starts_at_15_00_utc_of_the_day_before():
    periods = periods_at(datetime(2026, 10, 19, 15, 0, tzinfo=UTC))
    assert periods.day_start == kst(0)
    assert periods_at(datetime(2026, 10, 19, 14, 59, 59, tzinfo=UTC)).day_start == kst(0, day=19)


class FakeRepository:
    """Stands in for the database: usage per period and the member's last reservation."""

    def __init__(self, *, day=Usage(0, 0), six_hours=Usage(0, 0), last=None):
        self.day, self.six_hours = day, six_hours
        self.quota = SimpleNamespace(user_id=7, last_reserved_at=last)
        self.added = []

    def lock_quota(self, _session, user_id):
        assert user_id == 7
        return self.quota

    def day_usage(self, _session, _user_id, _day_start):
        return self.day

    def six_hour_usage(self, _session, _user_id, _six_hour_start):
        return self.six_hours

    def last_reserved_at(self, _session, _user_id):
        return self.quota.last_reserved_at

    def add_reservation(self, _session, quota_row, **values):
        self.added.append(values)
        quota_row.last_reserved_at = values["created_at"]
        return values


@pytest.fixture
def fake(monkeypatch):
    def install(**usage):
        repository = FakeRepository(**usage)
        for name in ("lock_quota", "day_usage", "six_hour_usage", "last_reserved_at", "add_reservation"):
            monkeypatch.setattr(quota.repository, name, getattr(repository, name))
        return repository

    return install


def test_reserve_records_the_periods_it_was_taken_in(fake):
    repository = fake(day=Usage(398, 1), six_hours=Usage(98, 1))
    now = kst(13, 5)
    reserved = quota.reserve(None, 7, now=now)
    assert reserved == {"day_start": kst(0), "six_hour_start": kst(12), "created_at": now}
    assert repository.quota.last_reserved_at == now


def test_reserved_generations_count_against_the_limit_like_used_ones(fake):
    fake(day=Usage(0, 0), six_hours=Usage(60, 40))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(13))
    assert rejected.value.reason == "quota_exceeded"


def test_an_empty_day_waits_until_kst_midnight(fake):
    fake(day=Usage(400, 0))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(23, 0))
    assert (rejected.value.reason, rejected.value.retry_after) == ("quota_exceeded", 3600)


def test_an_empty_six_hour_period_waits_until_its_reset_as_in_the_draft_example(fake):
    # 03:07:00Z with only the six-hour period empty: 21180 seconds until 09:00:00Z.
    fake(day=Usage(100, 0), six_hours=Usage(100, 0))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=datetime(2026, 10, 20, 3, 7, tzinfo=UTC))
    assert (rejected.value.reason, rejected.value.retry_after) == ("quota_exceeded", 21180)


def test_when_both_periods_are_empty_the_later_reset_is_reported(fake):
    fake(day=Usage(399, 1), six_hours=Usage(100, 0))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(13))
    assert rejected.value.retry_after == 11 * 3600


def test_new_attempts_are_at_least_ten_seconds_apart(fake):
    repository = fake(last=kst(9, 0, 0))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(9, 0, 3))
    assert (rejected.value.reason, rejected.value.retry_after) == ("attempt_too_soon", 7)
    assert repository.added == []
    quota.reserve(None, 7, now=kst(9, 0, 10))
    assert len(repository.added) == 1


def test_the_interval_is_checked_before_the_limits(fake):
    fake(day=Usage(400, 0), six_hours=Usage(100, 0), last=kst(9, 0, 0))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(9, 0, 1))
    assert (rejected.value.reason, rejected.value.retry_after) == ("attempt_too_soon", 9)


def test_limits_report_used_reserved_and_remaining_per_period(fake):
    fake(day=Usage(12, 2), six_hours=Usage(5, 2), last=kst(12, 0, 40))
    current = quota.limits(None, 7, now=kst(12, 0, 45))
    assert current.daily == quota.WindowLimit(400, 12, 2, 386, kst(0, day=21))
    assert current.six_hour == quota.WindowLimit(100, 5, 2, 93, kst(18))
    assert current.next_attempt_at == kst(12, 0, 50)
    assert quota.limits(None, 7, now=kst(12, 0, 50)).next_attempt_at is None


def test_remaining_never_goes_below_zero(fake):
    fake(six_hours=Usage(100, 1))
    assert quota.limits(None, 7, now=kst(7)).six_hour.remaining == 0


@pytest.mark.parametrize(
    ("state", "reason", "charged"),
    [
        ("settled", None, True),
        ("reserved", None, True),
        ("refunded", "failed", True),
        ("refunded", "interrupted", True),
        ("refunded", "canceled", False),
        ("refunded", "expired", False),
    ],
)
def test_provider_charge_is_reported_apart_from_the_member_refund(state, reason, charged):
    reservation = QuotaReservation(state=state, refund_reason=reason)
    assert provider_may_have_charged(reservation) is charged
