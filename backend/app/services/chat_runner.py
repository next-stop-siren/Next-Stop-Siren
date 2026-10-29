"""Run queued chat jobs: claim, call the provider outside any transaction, store the outcome (#52).

`run_next` is one execution of one member's next job in three steps: a short
transaction that takes the execution right and starts the job, the provider call with
no session open, and a short transaction that stores the outcome under the job's
`run_token`. If that last commit fails, the job stays running and `recover` interrupts
it after its deadline; the provider is never called again for it.

`ChatJobWorker` polls for members with work and runs each member's jobs one after
another on a small thread pool, so one member's generation never waits for another
member's. The database lock decides who runs; the worker's own bookkeeping only avoids
asking twice. The app starts the worker (#53) and turns its `notify` and `on_delta`,
which also receive the member's ID, into subscription events. `notify` is called only
after a commit, so a stored outcome that failed to commit is never announced. Log
entries carry row IDs only, never a question or an answer, and a failure is logged as
its `SafeError` classification, never its exception text or traceback (#70).
"""

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from sqlalchemy.orm import Session, sessionmaker

from app.integrations.provider import AnswerProvider, ContextMessage, ProviderFailed, ProviderTimeout
from app.observability import SafeError
from app.services import chat_queue
from app.services.chat import ANSWER_TIME_LIMIT_SECONDS
from app.services.chat_queue import OUTCOME_UNKNOWN, PROVIDER_FAILED, Outcome, QuotaLedger

logger = logging.getLogger(__name__)

Notify = Callable[[Sequence[int]], None]
OnDelta = Callable[[int, str], None]
# The worker's callbacks also receive the member whose jobs changed.
MemberNotify = Callable[[int, Sequence[int]], None]
MemberDelta = Callable[[int, int, str], None]


class _Ended:
    """Put on the queue after the provider's last piece."""


def generate(
    provider: AnswerProvider,
    context: Sequence[ContextMessage],
    *,
    time_limit: float = ANSWER_TIME_LIMIT_SECONDS,
    on_delta: Callable[[str], None] | None = None,
) -> Outcome:
    """Stream the answer within the time limit and return how it ended; pieces go to `on_delta` as they arrive.

    The provider is read on a helper thread, so a provider that stops sending cannot
    hold the job past the limit; a piece that arrives after it counts as a timeout.
    Any failure other than `ProviderFailed` leaves the outcome unknown.
    """
    deadline = time.monotonic() + time_limit
    arrivals: queue.Queue[tuple[str | _Ended | Exception, float]] = queue.Queue()
    stop = threading.Event()

    def receive() -> None:
        pieces: Iterator[str] | None = None
        try:
            pieces = provider.stream(context, timeout_seconds=time_limit)
            for piece in pieces:
                arrivals.put((piece, time.monotonic()))
                if stop.is_set():
                    return
            arrivals.put((_Ended(), time.monotonic()))
        except Exception as error:
            arrivals.put((error, time.monotonic()))
        finally:
            close = getattr(pieces, "close", None)
            if close is not None:
                close()

    threading.Thread(target=receive, daemon=True).start()
    parts: list[str] = []
    try:
        while True:
            try:
                item, arrived_at = arrivals.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return OUTCOME_UNKNOWN
            if arrived_at > deadline:
                return OUTCOME_UNKNOWN
            if isinstance(item, str):
                parts.append(item)
                if on_delta is not None:
                    on_delta(item)
                continue
            if isinstance(item, _Ended):
                text = "".join(parts)
                return Outcome("completed", text=text) if text.strip() else PROVIDER_FAILED
            if isinstance(item, ProviderFailed):
                return PROVIDER_FAILED
            if not isinstance(item, ProviderTimeout):
                logger.error("Provider raised an unexpected error, error_type=%s", type(item).__name__)
            return OUTCOME_UNKNOWN
    finally:
        stop.set()


def run_next(
    session_factory: Callable[[], Session],
    provider: AnswerProvider,
    ledger: QuotaLedger,
    user_id: int,
    *,
    time_limit: float = ANSWER_TIME_LIMIT_SECONDS,
    notify: Notify | None = None,
    on_delta: OnDelta | None = None,
) -> bool:
    """Run the member's next job if the execution right is free; False when nothing started."""
    with session_factory() as session:
        claimed, changes = chat_queue.claim_next(session, ledger, user_id)
        session.commit()
    if notify is not None and changes.job_ids:
        notify(changes.job_ids)
    if claimed is None:
        return False
    logger.info("Job started, job_id=%s, answer_id=%s", claimed.job_id, claimed.answer_message_id)
    called_at = time.monotonic()
    job_id = claimed.job_id
    delta = None if on_delta is None else (lambda text: on_delta(job_id, text))
    outcome = generate(provider, claimed.context, time_limit=time_limit, on_delta=delta)
    logger.info(
        "Provider call ended, job_id=%s, outcome=%s, elapsed_ms=%s",
        claimed.job_id,
        outcome.error_code or outcome.status,
        round((time.monotonic() - called_at) * 1000),
    )
    try:
        with session_factory() as session:
            finished = chat_queue.finish(session, ledger, claimed.job_id, claimed.run_token, outcome)
            session.commit()
    except Exception as error:
        # The outcome is not stored: the job stays running and recovery interrupts it later.
        logger.error(
            "Job outcome not stored, job_id=%s, outcome=%s, %s",
            claimed.job_id,
            outcome.error_code or outcome.status,
            SafeError.of(error),
        )
        raise
    if notify is not None and finished is not None:
        notify(finished.job_ids)
    return True


class ChatJobWorker:
    """Poll for members with work and drain each member's queue on its own pool thread."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: AnswerProvider,
        ledger: QuotaLedger,
        *,
        max_members: int = 4,
        poll_seconds: float = 1.0,
        notify: MemberNotify | None = None,
        on_delta: MemberDelta | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._ledger = ledger
        self._max_members = max_members
        self._poll_seconds = poll_seconds
        self._notify = notify
        self._on_delta = on_delta
        self._pool = ThreadPoolExecutor(max_workers=max_members, thread_name_prefix="chat-job")
        self._busy: set[int] = set()
        self._busy_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._loop: threading.Thread | None = None

    def start(self) -> None:
        self._loop = threading.Thread(target=self._run, name="chat-job-poll", daemon=True)
        self._loop.start()

    def wake(self) -> None:
        """Look for work now, for example right after an admission commits."""
        self._wake.set()

    def stop(self, *, timeout: float | None = None) -> None:
        """Stop polling and wait for running jobs; a job cut off here is recovered after a restart."""
        self._stopping.set()
        self._wake.set()
        if self._loop is not None:
            self._loop.join(timeout)
        self._pool.shutdown(wait=True)

    def tick(self) -> list[int]:
        """Dispatch the members with work that this worker is not already running; return them."""
        with self._session_factory() as session:
            members = chat_queue.members_with_work(session)
            session.rollback()
        dispatched = []
        with self._busy_lock:
            for user_id in members:
                if user_id in self._busy or len(self._busy) >= self._max_members:
                    continue
                self._busy.add(user_id)
                dispatched.append(user_id)
        for user_id in dispatched:
            self._pool.submit(self._drain, user_id)
        return dispatched

    def _drain(self, user_id: int) -> None:
        notify = None if self._notify is None else partial(self._notify, user_id)
        on_delta = None if self._on_delta is None else partial(self._on_delta, user_id)
        try:
            while not self._stopping.is_set():
                if not run_next(
                    self._session_factory,
                    self._provider,
                    self._ledger,
                    user_id,
                    notify=notify,
                    on_delta=on_delta,
                ):
                    break
        except Exception as error:
            # Never the exception text or traceback: a database error carries its SQL and bound values.
            logger.error("Chat job run failed, user_id=%s, %s", user_id, SafeError.of(error))
        finally:
            with self._busy_lock:
                self._busy.discard(user_id)

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception as error:
                logger.error("Chat job poll failed, %s", SafeError.of(error))
            self._wake.wait(self._poll_seconds)
            self._wake.clear()
