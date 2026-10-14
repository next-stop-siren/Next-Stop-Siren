"""Exercise login failure windows, resets, cleanup, and concurrent admission."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier, Event, Lock

import pytest

from app.security.login_limits import LoginFailureLimiter, LoginRateLimited

EMAIL = "fixture@example.test"
IP = "192.0.2.1"


@pytest.fixture
def clock():
    return [0.0]


@pytest.fixture
def limiter(clock):
    return LoginFailureLimiter(clock=lambda: clock[0])


def fail(limiter: LoginFailureLimiter, email: str = EMAIL, ip: str = IP) -> None:
    with limiter.attempt(email, ip) as attempt:
        attempt.failed()


def test_five_failures_are_allowed_and_the_next_attempt_is_blocked(limiter) -> None:
    for _ in range(5):
        fail(limiter)
    with pytest.raises(LoginRateLimited) as caught:
        with limiter.attempt(EMAIL, "192.0.2.2"):
            pytest.fail("같은 이메일은 IP가 바뀌어도 제한되어야 한다.")
    assert caught.value.retry_after == 600
    assert EMAIL not in str(caught.value) and IP not in str(caught.value)


def test_ip_limit_applies_across_different_emails(limiter) -> None:
    for index in range(30):
        fail(limiter, f"fixture-{index}@example.test")
    with pytest.raises(LoginRateLimited) as caught:
        with limiter.attempt("new@example.test", IP):
            pytest.fail("새 이메일도 같은 IP의 제한을 따라야 한다.")
    assert caught.value.retry_after == 600
    with limiter.attempt("new@example.test", "192.0.2.2"):
        pass


def test_window_slides_and_wait_is_rounded_up(limiter, clock) -> None:
    for index in range(5):
        clock[0] = float(index * 100)
        fail(limiter)
    clock[0] = 599.1
    with pytest.raises(LoginRateLimited) as caught:
        with limiter.attempt(EMAIL, IP):
            pass
    assert caught.value.retry_after == 1
    clock[0] = 600.0
    fail(limiter)
    with pytest.raises(LoginRateLimited) as caught:
        with limiter.attempt(EMAIL, IP):
            pass
    assert caught.value.retry_after == 100


def test_blocked_requests_do_not_extend_the_failure_window(limiter, clock) -> None:
    for _ in range(5):
        fail(limiter)
    for second in (100, 200, 599):
        clock[0] = float(second)
        with pytest.raises(LoginRateLimited) as caught:
            with limiter.attempt(EMAIL, IP):
                pass
        assert caught.value.retry_after == 600 - second
    clock[0] = 600
    with limiter.attempt(EMAIL, IP):
        pass


def test_both_limits_use_the_longer_wait(limiter, clock) -> None:
    for _ in range(5):
        fail(limiter, EMAIL, "192.0.2.2")
    clock[0] = 100
    for index in range(30):
        fail(limiter, f"other-{index}@example.test", IP)
    clock[0] = 200
    with pytest.raises(LoginRateLimited) as caught:
        with limiter.attempt(EMAIL, IP):
            pass
    assert caught.value.retry_after == 500


def test_success_resets_only_the_matching_email(limiter) -> None:
    for _ in range(4):
        fail(limiter)
    for _ in range(5):
        fail(limiter, "other@example.test", "192.0.2.2")
    with limiter.attempt(EMAIL, IP) as attempt:
        attempt.succeeded()
    for _ in range(5):
        fail(limiter)
    with pytest.raises(LoginRateLimited):
        with limiter.attempt("other@example.test", "192.0.2.3"):
            pass
    # 첫 4회가 IP 기록에는 남아 있으므로 21회를 더하면 IP 한도에 도달한다.
    for index in range(21):
        fail(limiter, f"new-{index}@example.test")
    with pytest.raises(LoginRateLimited):
        with limiter.attempt("fresh@example.test", IP):
            pass


def test_internal_errors_release_reservations_without_counting_failures(limiter) -> None:
    for _ in range(40):
        with pytest.raises(RuntimeError):
            with limiter.attempt(EMAIL, IP):
                raise RuntimeError("가짜 내부 오류")
    for _ in range(5):
        fail(limiter)


def test_failure_is_counted_at_completion(limiter, clock) -> None:
    with limiter.attempt(EMAIL, IP) as attempt:
        clock[0] = 600
        attempt.failed()
    for _ in range(4):
        fail(limiter)
    with pytest.raises(LoginRateLimited) as caught:
        with limiter.attempt(EMAIL, IP):
            pass
    assert caught.value.retry_after == 600


def test_expired_and_empty_entries_are_removed(limiter, clock) -> None:
    for index in range(100):
        fail(limiter, f"fixture-{index}@example.test", f"192.0.2.{index + 1}")
    clock[0] = 600
    with limiter.attempt(EMAIL, IP):
        pass
    assert limiter._emails == {} and limiter._ips == {}


def test_pending_attempts_reserve_slots_and_release_them(limiter) -> None:
    with ExitStack() as stack:
        for _ in range(5):
            stack.enter_context(limiter.attempt(EMAIL, IP))
        with pytest.raises(LoginRateLimited) as caught:
            with limiter.attempt(EMAIL, IP):
                pass
        assert caught.value.retry_after == 1
        # 다른 이메일·IP의 검사는 전역 잠금 때문에 대기하지 않는다.
        with limiter.attempt("other@example.test", "192.0.2.2"):
            pass
    with limiter.attempt(EMAIL, IP):
        pass


def test_success_does_not_discard_other_inflight_failures(limiter) -> None:
    with limiter.attempt(EMAIL, IP) as pending:
        with limiter.attempt(EMAIL, IP) as successful:
            successful.succeeded()
        pending.failed()
    for _ in range(4):
        fail(limiter)
    with pytest.raises(LoginRateLimited):
        with limiter.attempt(EMAIL, IP):
            pass


@pytest.mark.parametrize("first,second", [("2001:db8::1", "2001:0db8:0:0:0:0:0:1"), (IP, "::ffff:192.0.2.1")])
def test_equivalent_ip_spellings_share_a_limit(limiter, first: str, second: str) -> None:
    for index in range(30):
        fail(limiter, f"fixture-{index}@example.test", first)
    with pytest.raises(LoginRateLimited):
        with limiter.attempt("new@example.test", second):
            pass


@pytest.mark.parametrize("same_email,workers,allowed", [(True, 20, 5), (False, 40, 30)])
def test_concurrent_attempts_cannot_overbook_limits(limiter, same_email: bool, workers: int, allowed: int) -> None:
    start = Barrier(workers)
    release = Event()
    checked = Event()
    lock = Lock()
    count = 0

    def mark_checked() -> None:
        nonlocal count
        with lock:
            count += 1
            if count == workers:
                checked.set()

    def worker(index: int) -> bool:
        start.wait(timeout=10)
        email = EMAIL if same_email else f"fixture-{index}@example.test"
        try:
            with limiter.attempt(email, IP) as attempt:
                mark_checked()
                assert release.wait(timeout=10)
                attempt.failed()
            return True
        except LoginRateLimited:
            mark_checked()
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, index) for index in range(workers)]
        try:
            assert checked.wait(timeout=10)
        finally:
            release.set()
        assert sum(future.result(timeout=10) for future in futures) == allowed
