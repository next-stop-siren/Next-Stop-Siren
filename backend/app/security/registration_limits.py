"""Atomic, process-local registration admission without a waiting queue."""

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from math import ceil
from threading import Lock
from time import monotonic

from app.security.settings import RegistrationLimitSettings


class RegistrationRateLimited(Exception):
    """접수량 제한이 풀릴 때까지 남은 초를 전달한다."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("가입 요청이 많습니다. 잠시 후 다시 시도해 주세요.")


class RegistrationBusy(Exception):
    """처리 자리가 없으면 접수 횟수를 소모하지 않고 즉시 거절한다."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("다른 가입 요청을 처리하고 있습니다. 잠시 후 다시 시도해 주세요.")


class RegistrationLimiter:
    """시간창의 접수 기록과 진행 중인 가입 수를 하나의 잠금으로 관리한다.

    인스턴스를 앱에 한 번 생성해 공유한다. 여러 worker·복제본 사이에는 공유되지 않는다.
    호출자는 실제 동기 해시부터 저장·쿠키 설정·실패 보상 종료까지 attempt 안에서 수행해야 한다.
    연결 종료만으로 attempt를 벗어나거나 실제 작업을 별도 스레드에 남겨 두면 안 된다.
    """

    def __init__(self, settings: RegistrationLimitSettings, *, clock: Callable[[], float] = monotonic) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = Lock()
        self._admissions: deque[float] = deque()
        self._active = 0

    @contextmanager
    def attempt(self) -> Iterator[None]:
        """접수와 자리 확보를 함께 수행하며, 실제 작업 종료 때만 자리를 반환한다."""
        with self._lock:
            now = self._clock()
            while self._admissions and self._admissions[0] <= now - self._settings.window_seconds:
                self._admissions.popleft()
            # 접수량과 처리 상한에 모두 걸리면 시간창의 제한을 우선한다.
            if len(self._admissions) >= self._settings.limit:
                retry_after = max(1, ceil(self._admissions[0] + self._settings.window_seconds - now))
                raise RegistrationRateLimited(retry_after)
            if self._active >= self._settings.max_concurrent:
                raise RegistrationBusy(self._settings.busy_retry_seconds)
            self._admissions.append(now)
            self._active += 1
        try:
            # 해시·DB 작업을 하는 동안 전역 잠금을 잡거나 다음 요청을 대기시키지 않는다.
            yield
        finally:
            with self._lock:
                # 실패·취소도 접수 횟수에는 남기되, 처리 자리는 반드시 반환한다.
                self._active -= 1
