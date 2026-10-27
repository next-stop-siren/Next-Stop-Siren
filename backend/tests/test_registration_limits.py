"""Registration windows, rejected admissions, slot lifetime and threaded contention."""

from asyncio import CancelledError
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from app.main import create_app
from app.security.registration_limits import RegistrationBusy, RegistrationLimiter, RegistrationRateLimited
from app.security.settings import RegistrationLimitSettings


@pytest.fixture
def clock():
    return [0.0]


@pytest.fixture
def limiter(clock):
    return RegistrationLimiter(RegistrationLimitSettings(), clock=lambda: clock[0])


def admit(limiter):
    with limiter.attempt():
        pass


def test_approved_ten_admissions_and_rate_limit(limiter):
    for _ in range(10):
        admit(limiter)
    with pytest.raises(RegistrationRateLimited) as caught:
        admit(limiter)
    assert caught.value.retry_after == 600


def test_busy_rejections_do_not_consume_admissions(limiter):
    with limiter.attempt():
        for _ in range(20):
            with pytest.raises(RegistrationBusy) as caught:
                admit(limiter)
            assert caught.value.retry_after == 5
    # Only the held attempt was admitted; all nine remaining admissions are available.
    for _ in range(9):
        admit(limiter)
    with pytest.raises(RegistrationRateLimited):
        admit(limiter)


def test_sliding_window_rounds_up_and_expires_at_exact_boundary(clock):
    limiter = RegistrationLimiter(RegistrationLimitSettings(limit=2), clock=lambda: clock[0])
    admit(limiter)
    clock[0] = 100.0
    admit(limiter)
    clock[0] = 598.1
    with pytest.raises(RegistrationRateLimited) as caught:
        admit(limiter)
    assert caught.value.retry_after == 2
    clock[0] = 599.9
    with pytest.raises(RegistrationRateLimited) as caught:
        admit(limiter)
    assert caught.value.retry_after == 1
    clock[0] = 600.0
    admit(limiter)
    with pytest.raises(RegistrationRateLimited) as caught:
        admit(limiter)
    # Rejections did not move the second admission from t=100.
    assert caught.value.retry_after == 100


def test_expired_admission_does_not_release_still_running_work(clock):
    limiter = RegistrationLimiter(RegistrationLimitSettings(limit=1), clock=lambda: clock[0])
    with limiter.attempt():
        clock[0] = 600.0
        with pytest.raises(RegistrationBusy):
            admit(limiter)
    admit(limiter)


@pytest.mark.parametrize("error", [RuntimeError, CancelledError])
def test_error_or_cancellation_releases_slot_but_keeps_admission(error):
    limiter = RegistrationLimiter(RegistrationLimitSettings(limit=2))
    with pytest.raises(error):
        with limiter.attempt():
            raise error()
    admit(limiter)
    with pytest.raises(RegistrationRateLimited):
        admit(limiter)


def test_rate_limit_takes_precedence_when_all_slots_are_busy():
    limiter = RegistrationLimiter(RegistrationLimitSettings(limit=1))
    with limiter.attempt():
        with pytest.raises(RegistrationRateLimited):
            admit(limiter)


def test_failed_work_returns_only_its_own_slot():
    limiter = RegistrationLimiter(RegistrationLimitSettings(max_concurrent=2))
    with limiter.attempt():
        with pytest.raises(RuntimeError):
            with limiter.attempt():
                raise RuntimeError("synthetic failure")
        with limiter.attempt():
            with pytest.raises(RegistrationBusy):
                admit(limiter)
    admit(limiter)


def test_custom_concurrency_window_and_busy_retry(clock):
    limiter = RegistrationLimiter(
        RegistrationLimitSettings(limit=4, window_seconds=30, max_concurrent=2, busy_retry_seconds=7),
        clock=lambda: clock[0],
    )
    with limiter.attempt(), limiter.attempt():
        with pytest.raises(RegistrationBusy) as caught:
            admit(limiter)
        assert caught.value.retry_after == 7
    admit(limiter)
    admit(limiter)
    with pytest.raises(RegistrationRateLimited) as caught:
        admit(limiter)
    assert caught.value.retry_after == 30
    clock[0] = 30.0
    admit(limiter)


@pytest.mark.parametrize("limit,slots,expected", [(10, 1, 1), (10, 3, 3), (2, 3, 2)])
def test_threaded_contention_never_exceeds_either_limit(limit, slots, expected):
    limiter = RegistrationLimiter(RegistrationLimitSettings(limit=limit, max_concurrent=slots))
    start = Barrier(21)
    rejected = Event()
    release = Event()
    lock = Lock()
    active = peak = rejected_count = 0

    def run():
        nonlocal active, peak, rejected_count
        start.wait(timeout=10)
        try:
            with limiter.attempt():
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    assert release.wait(timeout=10)
                finally:
                    with lock:
                        active -= 1
                return "admitted"
        except (RegistrationBusy, RegistrationRateLimited) as exc:
            with lock:
                rejected_count += 1
                if rejected_count == 20 - expected:
                    rejected.set()
            return "busy" if isinstance(exc, RegistrationBusy) else "rate_limited"

    with ThreadPoolExecutor(max_workers=20) as workers:
        futures = [workers.submit(run) for _ in range(20)]
        try:
            start.wait(timeout=10)
            # Every excess attempt must finish while admitted work is still blocked.
            assert rejected.wait(timeout=10)
            with lock:
                assert active == peak == expected
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert results.count("admitted") == expected
    assert results.count("busy") == (20 - expected if limit > expected else 0)
    assert results.count("rate_limited") == (20 - expected if limit == expected else 0)
    assert active == 0
    # Rejected threads consumed no quota and all acquired slots have been returned.
    for _ in range(limit - expected):
        admit(limiter)
    with pytest.raises(RegistrationRateLimited):
        admit(limiter)


def test_app_shares_one_limiter_and_separate_apps_do_not_share_state(auth_settings):
    settings = RegistrationLimitSettings(limit=1)
    application = create_app(auth_settings, registration_limit_settings=settings)
    limiter = application.state.registration_limiter
    assert isinstance(limiter, RegistrationLimiter)
    admit(limiter)
    with pytest.raises(RegistrationRateLimited):
        admit(application.state.registration_limiter)
    other = create_app(auth_settings, registration_limit_settings=settings)
    admit(other.state.registration_limiter)


def test_invalid_settings_do_not_create_unlimited_limiter(monkeypatch, auth_settings):
    monkeypatch.setenv("AUTH_REGISTER_LIMIT", "0")
    application = create_app(auth_settings)
    assert application.state.registration_limiter is None
