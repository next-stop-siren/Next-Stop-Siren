"""Member subscriptions of the chat queue (`GET /api/chat/events`) and the worker that feeds them (#53).

One `ChatEvents` per app keeps the open subscriptions of this process. A change is
published only after its transaction commits: the publisher reads the committed rows
again and sends a `job` event per changed or related job, then a `limits` event. An
outcome whose commit failed is therefore never announced, and a `completed` event
always carries the answer as stored.

Publishing and opening a subscription take the member's publish lock around their
reads, so the events of one member reach each subscription in the order they were
read, and the `snapshot` comes first. A subscription is not the owner of any work:
closing it, or logging out, leaves every job running. Missed `delta` pieces are not
replayed; a reconnect gets the stored results through `since`.

Subscriptions live in this process only. With several server processes, a
subscriber still receives what the other processes changed through the next
reconnect's snapshot, not as live events.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.chat_payloads import item_out, limits_out
from app.api.schemas import AnswerDelta, Snapshot
from app.repositories import chat_jobs as repository
from app.services import chat_views
from app.services.chat_runner import ChatJobWorker

logger = logging.getLogger(__name__)

PING_SECONDS = 15.0
# A subscriber this far behind is closed; it reconnects with `since` and loses nothing stored.
MAX_PENDING = 500

SessionOpener = Callable[[], AbstractContextManager[Session]]


def sse(event: str, data: BaseModel) -> str:
    """One server-sent event; the JSON is a single line because JSON escapes line breaks."""
    return f"event: {event}\ndata: {data.model_dump_json()}\n\n"


class Closed:
    """Delivered last to a subscription that fell too far behind."""


CLOSED = Closed()


class Subscription:
    """The events of one member for one open connection, delivered on the connection's event loop."""

    def __init__(self, user_id: int, loop: asyncio.AbstractEventLoop) -> None:
        self.user_id = user_id
        self._loop = loop
        self._queue: asyncio.Queue[str | Closed] = asyncio.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._closed = False

    def put(self, frame: str) -> None:
        """Queue a frame from any thread; a subscription that is too far behind gets `CLOSED` instead."""
        item: str | Closed = frame
        with self._lock:
            if self._closed:
                return
            self._pending += 1
            if self._pending > MAX_PENDING:
                self._closed = True
                item = CLOSED
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            # The connection's loop has already closed.
            with self._lock:
                self._closed = True

    async def get(self) -> str | Closed:
        item = await self._queue.get()
        with self._lock:
            self._pending -= 1
        return item


@dataclass(frozen=True)
class Opened:
    subscription: Subscription
    # The earliest quota reset after the snapshot; a `limits` event is due then.
    next_reset: datetime


class ChatEvents:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[int, set[Subscription]] = {}
        self._member_locks: dict[int, threading.Lock] = {}
        self._sessions: SessionOpener | None = None
        self._worker: ChatJobWorker | None = None

    # The worker -------------------------------------------------------------

    def attach(self, sessions: SessionOpener, worker: ChatJobWorker) -> None:
        self._sessions = sessions
        self._worker = worker

    @property
    def attached(self) -> bool:
        return self._worker is not None

    def detach(self) -> None:
        self._worker = None

    def wake(self) -> None:
        """Ask the worker to look for work now, for example right after an admission commits."""
        if self._worker is not None:
            self._worker.wake()

    # Subscriptions ----------------------------------------------------------

    def _member_lock(self, user_id: int) -> threading.Lock:
        with self._lock:
            return self._member_locks.setdefault(user_id, threading.Lock())

    def _current(self, user_id: int) -> list[Subscription]:
        with self._lock:
            return list(self._subscribers.get(user_id, ()))

    def subscribed(self, user_id: int) -> bool:
        return bool(self._current(user_id))

    def open(self, session: Session, user_id: int, loop: asyncio.AbstractEventLoop, since: datetime | None) -> Opened:
        """Subscribe the member and queue the snapshot as the first event."""
        subscription = Subscription(user_id, loop)
        with self._member_lock(user_id):
            with self._lock:
                self._subscribers.setdefault(user_id, set()).add(subscription)
            try:
                found = chat_views.snapshot(session, user_id, since)
            except BaseException:
                self.close(subscription)
                raise
            limits = limits_out(found.limits)
            body = Snapshot(
                items=[item_out(item) for item in found.items], limits=limits, server_time=found.server_time
            )
            subscription.put(sse("snapshot", body))
        return Opened(subscription, min(limits.daily.resets_at, limits.six_hour.resets_at))

    def close(self, subscription: Subscription) -> None:
        with self._lock:
            subscribers = self._subscribers.get(subscription.user_id)
            if subscribers is not None:
                subscribers.discard(subscription)
                if not subscribers:
                    del self._subscribers[subscription.user_id]

    def refresh_limits(self, session: Session, subscription: Subscription) -> datetime:
        """Send the current `limits` to one subscription, after a quota period reset; return the next reset."""
        with self._member_lock(subscription.user_id):
            limits = limits_out(chat_views.limits(session, subscription.user_id))
            subscription.put(sse("limits", limits))
        return min(limits.daily.resets_at, limits.six_hour.resets_at)

    # Publishing -------------------------------------------------------------

    def publish(self, session: Session, user_id: int, job_ids: Sequence[int]) -> None:
        """Send the committed state of the changed jobs, the jobs they affect and the limits; never raises.

        Call it after the commit, on a session whose next reads see that commit.
        """
        if not job_ids or not self.subscribed(user_id):
            return
        try:
            with self._member_lock(user_id):
                ids = chat_views.related_ids(session, user_id, job_ids)
                jobs = repository.owned_jobs(session, user_id, ids)
                frames = [sse("job", item_out(found)) for found in chat_views.items(session, user_id, jobs)]
                frames.append(sse("limits", limits_out(chat_views.limits(session, user_id))))
                for subscription in self._current(user_id):
                    for frame in frames:
                        subscription.put(frame)
        except Exception:
            # The change is stored; a subscriber sees it at its next reconnect.
            logger.exception("Chat event publish failed, user_id=%s", user_id)

    def notify(self, user_id: int, job_ids: Sequence[int]) -> None:
        """The worker's `notify`: publish on a session of its own."""
        if self._sessions is None or not self.subscribed(user_id):
            return
        try:
            with self._sessions() as session:
                self.publish(session, user_id, job_ids)
                session.rollback()
        except Exception:
            logger.exception("Chat event session failed, user_id=%s", user_id)

    def delta(self, user_id: int, job_id: int, text: str) -> None:
        """The worker's `on_delta`: temporary output that is never stored or replayed."""
        subscriptions = self._current(user_id)
        if subscriptions:
            frame = sse("delta", AnswerDelta(job_id=str(job_id), text=text))
            for subscription in subscriptions:
                subscription.put(frame)
