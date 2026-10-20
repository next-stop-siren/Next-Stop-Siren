"""Unit checks for member quota periods, admission order and provider charge reporting."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.quota import QuotaReservation
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
    """Stands in for the database: counts per period and the member's last reservation."""

    def __init__(self, *, day=0, six_hours=0, last=None):
        self.day, self.six_hours = day, six_hours
        self.quota = SimpleNamespace(user_id=7, last_reserved_at=last)
        self.added = []

    def lock_quota(self, _session, user_id):
        assert user_id == 7
        return self.quota

    def count_day(self, _session, _user_id, _day_start):
        return self.day

    def count_six_hours(self, _session, _user_id, _six_hour_start):
        return self.six_hours

    def last_reserved_at(self, _session, _user_id):
        return self.quota.last_reserved_at

    def add_reservation(self, _session, quota_row, **values):
        self.added.append(values)
        quota_row.last_reserved_at = values["created_at"]
        return values


@pytest.fixture
def fake(monkeypatch):
    def install(**counts):
        repository = FakeRepository(**counts)
        for name in ("lock_quota", "count_day", "count_six_hours", "last_reserved_at", "add_reservation"):
            monkeypatch.setattr(quota.repository, name, getattr(repository, name))
        return repository

    return install


def test_reserve_records_the_periods_it_was_taken_in(fake):
    repository = fake(day=399, six_hours=99)
    now = kst(13, 5)
    reserved = quota.reserve(None, 7, now=now)
    assert reserved == {"day_start": kst(0), "six_hour_start": kst(12), "created_at": now}
    assert repository.quota.last_reserved_at == now


def test_the_daily_limit_waits_until_kst_midnight(fake):
    fake(day=400, six_hours=0)
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(23, 0))
    assert (rejected.value.reason, rejected.value.retry_after) == ("daily_limit", 3600)


def test_the_six_hour_limit_waits_until_the_period_resets(fake):
    fake(day=100, six_hours=100)
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(17, 59, 30))
    assert (rejected.value.reason, rejected.value.retry_after) == ("six_hour_limit", 30)


def test_new_attempts_are_at_least_ten_seconds_apart(fake):
    repository = fake(last=kst(9, 0, 0))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(9, 0, 3))
    assert (rejected.value.reason, rejected.value.retry_after) == ("too_soon", 7)
    assert repository.added == []
    quota.reserve(None, 7, now=kst(9, 0, 10))
    assert len(repository.added) == 1


def test_a_limit_is_reported_before_the_interval(fake):
    fake(day=0, six_hours=100, last=kst(9, 0, 0))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(None, 7, now=kst(9, 0, 1))
    assert rejected.value.reason == "six_hour_limit"


def test_status_reports_what_is_left_and_when_it_resets(fake):
    fake(day=150, six_hours=100, last=kst(19, 0, 0))
    current = quota.status(None, 7, now=kst(19, 0, 4))
    assert (current.daily_remaining, current.six_hour_remaining) == (250, 0)
    assert (current.daily_resets_at, current.six_hour_resets_at) == (kst(0, day=21), kst(0, day=21))
    assert current.next_attempt_at == kst(19, 0, 10)
    assert quota.status(None, 7, now=kst(19, 0, 10)).next_attempt_at is None


@pytest.mark.parametrize(
    ("state", "reason", "charged"),
    [
        ("settled", None, True),
        ("reserved", None, True),
        ("refunded", "failed", True),
        ("refunded", "interrupted", True),
        ("refunded", "cancelled", False),
        ("refunded", "expired", False),
    ],
)
def test_provider_charge_is_reported_apart_from_the_member_refund(state, reason, charged):
    reservation = QuotaReservation(state=state, refund_reason=reason)
    assert provider_may_have_charged(reservation) is charged
