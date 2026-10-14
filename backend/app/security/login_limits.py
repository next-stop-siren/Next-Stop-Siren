"""Atomic, process-local limits for failed local login attempts."""

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from ipaddress import IPv6Address, ip_address
from math import ceil
from threading import Lock
from time import monotonic
from typing import Literal

EMAIL_FAILURE_LIMIT = 5
IP_FAILURE_LIMIT = 30
FAILURE_WINDOW_SECONDS = 600


class LoginRateLimited(Exception):
    """제한이 풀릴 때까지 기다릴 시간을 전달하며, 이메일과 IP는 포함하지 않는다."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.")


@dataclass(slots=True)
class _Bucket:
    failures: deque[float] = field(default_factory=deque)
    pending: int = 0


@dataclass(slots=True)
class LoginAttempt:
    """자격 증명 검사 결과를 기록한다. 내부 오류는 결과를 기록하지 않고 종료한다."""

    _outcome: Literal["failed", "succeeded"] | None = field(default=None, init=False, repr=False)

    def failed(self) -> None:
        """계정이 없거나 비밀번호가 틀렸을 때 호출한다."""
        self._outcome = "failed"

    def succeeded(self) -> None:
        """로그인이 성공했을 때 호출한다."""
        self._outcome = "succeeded"


class LoginFailureLimiter:
    """최근 10분의 실패와 진행 중인 검사를 한 프로세스 안에서 함께 관리한다.

    이메일 키는 DB의 lower(email) 결과를 받아 사용자 조회와 같은 비교 기준을 사용한다.
    여러 worker나 서버 사이에는 상태가 공유되지 않으므로 운영 공유 제한을 대신할 수 없다.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._lock = Lock()
        self._emails: dict[str, _Bucket] = {}
        self._ips: dict[str, _Bucket] = {}

    def _cleanup(self, now: float) -> None:
        """오래된 실패와 비어 있는 항목을 정리한다. 정확히 10분 된 실패는 제외한다."""
        for buckets in (self._emails, self._ips):
            for key, bucket in list(buckets.items()):
                while bucket.failures and bucket.failures[0] <= now - FAILURE_WINDOW_SECONDS:
                    bucket.failures.popleft()
                if not bucket.failures and not bucket.pending:
                    del buckets[key]

    @staticmethod
    def _wait(bucket: _Bucket | None, limit: int, now: float) -> int:
        if bucket is None or len(bucket.failures) + bucket.pending < limit:
            return 0
        # 진행 중인 검사가 끝나면 자리가 생길 수 있으므로 1초 뒤 다시 확인한다.
        if len(bucket.failures) < limit:
            return 1
        return max(1, ceil(bucket.failures[0] + FAILURE_WINDOW_SECONDS - now))

    @contextmanager
    def attempt(self, email_key: str, client_ip: str) -> Iterator[LoginAttempt]:
        """검사 자리를 예약하고, 종료 시 성공·실패에 맞춰 두 카운터를 함께 갱신한다."""
        address = ip_address(client_ip)
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        ip_key = str(address)
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            wait = max(
                self._wait(self._emails.get(email_key), EMAIL_FAILURE_LIMIT, now),
                self._wait(self._ips.get(ip_key), IP_FAILURE_LIMIT, now),
            )
            if wait:
                raise LoginRateLimited(wait)
            email = self._emails.setdefault(email_key, _Bucket())
            ip = self._ips.setdefault(ip_key, _Bucket())
            email.pending += 1
            ip.pending += 1
        result = LoginAttempt()
        try:
            # 비밀번호 검증과 DB 작업 동안에는 전역 잠금을 잡지 않는다.
            yield result
        finally:
            with self._lock:
                now = self._clock()
                email.pending -= 1
                ip.pending -= 1
                if result._outcome == "failed":
                    email.failures.append(now)
                    ip.failures.append(now)
                elif result._outcome == "succeeded":
                    # IP 실패와 같은 이메일의 다른 진행 중인 검사는 유지한다.
                    email.failures.clear()
                self._cleanup(now)
