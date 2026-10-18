"""Store a question, ask the provider for its answer and store the outcome.

The question and its pending answer are committed before the provider is called,
and the outcome is saved in a second transaction, so no transaction stays open
during the call. A pending answer that is never finished, for example after a
crash between the two, is closed by the next send to the same conversation.
A streamed answer follows the same two transactions; its pieces are never stored,
and its provider is held to the time limit by the service itself.

Refusals are raised as the business errors below; the API layer turns them into
HTTP responses. Log entries carry the caller's trace ID and row IDs only, never a
question, a request key, an answer or a provider's error message.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.integrations.provider import AnswerProvider, ContextMessage, ProviderFailed, ProviderTimeout
from app.models.conversation import Message
from app.repositories import conversations as repository

logger = logging.getLogger(__name__)

# The approved S10 limits in docs/04-reference/api.md. A pending answer must stay
# open longer than the provider is allowed to take.
ANSWER_TIME_LIMIT_SECONDS = 30
STALE_PENDING_SECONDS = 60


class ChatError(Exception):
    """A send refused before anything new is stored."""


class ConversationNotFound(ChatError):
    """The conversation does not exist or belongs to another user; the two are not distinguishable."""


class RequestKeyConflict(ChatError):
    """The request key is already stored with a different question."""


class ConversationBusy(ChatError):
    """Another question's answer is still pending."""


@dataclass(frozen=True)
class Exchange:
    question: Message
    answer: Message
    # False when the request key was already stored and no provider call was made.
    created: bool


@dataclass(frozen=True)
class Started:
    """A new question committed with its pending answer; the provider has not been called."""

    conversation_id: int
    question: Message
    answer: Message
    # Read before the commit: a session that expires rows on commit would reload them
    # and so reopen a transaction for the duration of the provider call.
    answer_id: int
    context: list[ContextMessage]


def begin_question(
    session: Session, user_id: int, conversation_id: int, content: str, request_key: str, *, trace_id: str
) -> Exchange | Started:
    """Return the stored pair of a request key already seen, or store the new question.

    `trace_id` ties this send's log entries to the caller's error response.
    """
    logger.info("Question received, trace_id=%s, conversation_id=%s", trace_id, conversation_id)
    if not repository.lock_conversation(session, user_id, conversation_id):
        raise ConversationNotFound
    closed = repository.close_stale_pending(session, conversation_id, STALE_PENDING_SECONDS)
    if closed:
        logger.warning("Stale pending answer interrupted, trace_id=%s, conversation_id=%s", trace_id, conversation_id)

    question = repository.find_question(session, conversation_id, request_key)
    if question is not None:
        if question.content != content:
            raise RequestKeyConflict
        answer = repository.latest_answer(session, question.id)
        logger.info(
            "Stored question returned, trace_id=%s, answer_id=%s, status=%s", trace_id, answer.id, answer.status
        )
        # Keeps a stale answer closed.
        session.commit()
        return Exchange(question, answer, created=False)
    if repository.has_pending_answer(session, conversation_id):
        raise ConversationBusy

    stored = repository.list_context_messages(session, user_id, conversation_id) or []
    context = [ContextMessage(message.role, message.content or "") for message in stored]
    context.append(ContextMessage("user", content))
    question, answer = repository.add_question(session, conversation_id, content, request_key)
    started = Started(conversation_id, question, answer, answer.id, context)
    session.commit()
    logger.info("Question saved, trace_id=%s, answer_id=%s", trace_id, started.answer_id)
    return started


def _store_outcome(
    session: Session,
    started: Started,
    status: str,
    text: str | None,
    error_code: str | None,
    *,
    called_at: float,
    trace_id: str,
) -> Message:
    """Store how the provider call ended and return the answer as stored, which recovery may have closed first.

    `called_at` is the `time.monotonic()` reading taken when the provider was called.
    """
    answer_id = started.answer_id
    log = logger.info if status == "completed" else logger.warning
    log(
        "Provider call ended, trace_id=%s, answer_id=%s, outcome=%s, elapsed_ms=%s",
        trace_id,
        answer_id,
        error_code or status,
        round((time.monotonic() - called_at) * 1000),
    )
    answer = repository.finish_answer(
        session, started.conversation_id, answer_id, status=status, content=text, error_code=error_code
    )
    stored_status = answer.status
    session.commit()
    logger.info("Answer saved, trace_id=%s, answer_id=%s, status=%s", trace_id, answer_id, stored_status)
    return answer


def send_question(
    session: Session,
    provider: AnswerProvider,
    user_id: int,
    conversation_id: int,
    content: str,
    request_key: str,
    *,
    trace_id: str,
) -> Exchange:
    """Answer the question with one provider call and return the pair once the answer is stored."""
    begun = begin_question(session, user_id, conversation_id, content, request_key, trace_id=trace_id)
    if isinstance(begun, Exchange):
        return begun
    text = None
    status, error_code = "completed", None
    called_at = time.monotonic()
    try:
        text = provider.complete(begun.context, timeout_seconds=ANSWER_TIME_LIMIT_SECONDS)
    except ProviderFailed:
        status, error_code = "failed", "provider_failed"
    except ProviderTimeout:
        status, error_code = "interrupted", "outcome_unknown"
    else:
        if not text.strip():
            text, status, error_code = None, "failed", "provider_failed"
    answer = _store_outcome(session, begun, status, text, error_code, called_at=called_at, trace_id=trace_id)
    return Exchange(begun.question, answer, created=True)


class _Ended:
    """Put on the queue after the provider's last piece."""


class _Left:
    """Put on the queue by `close`, to wake a reader that is waiting for the provider."""


# What goes on the queue, with the `time.monotonic()` reading it arrived at.
Arrival = tuple[str | _Ended | _Left | Exception, float]


class AnswerStream:
    """The pieces of a new answer as they arrive, then the answer as stored.

    A piece is temporary output. Nothing is stored until the provider has ended, and
    the answer is the last item, read only after its commit, so a reader that has not
    received it cannot take the answer as complete. A failure, or the end of the time
    limit, before the provider has ended stores no text.

    The time limit covers the whole stream, from the call to the provider's end. The
    provider is read on a worker thread, so a provider that stops sending cannot hold
    the stream past it; an arrival after it counts as a timeout.

    `close` ends the stream at any point: before the first piece, between two pieces,
    while the reader waits for the provider's end, or after the end. It may be called
    from another thread while a `next` is waiting. An answer whose outcome has not been
    stored yet is then stored as interrupted, never as completed: the reader checks for
    `close` right before it stores. `open_session` gives the session of that one short
    transaction.
    """

    def __init__(
        self,
        open_session: Callable[[], AbstractContextManager[Session]],
        provider: AnswerProvider,
        started: Started,
        *,
        trace_id: str,
    ) -> None:
        self._open_session = open_session
        self._provider = provider
        self._started = started
        self._trace_id = trace_id
        self._called_at = time.monotonic()
        self._arrivals: queue.Queue[Arrival] = queue.Queue()
        # Set by `close`; the worker stops asking the provider and the reader stores no completion.
        self._stop = threading.Event()
        # Held while `next` runs, so `close` waits for a running `next` instead of overlapping it.
        self._reading = threading.Lock()
        # Set once the outcome is handed to storage, which then is not tried again.
        self._storing = False
        self._items = self._read()

    def __iter__(self) -> "AnswerStream":
        return self

    def __next__(self) -> str | Message:
        with self._reading:
            return next(self._items)

    def close(self) -> None:
        """Stop reading; an answer whose outcome is not stored yet is stored as interrupted.

        If storing fails, the answer stays pending and the next send recovers it.
        """
        self._stop.set()
        self._arrivals.put((_Left(), time.monotonic()))
        with self._reading:
            self._items.close()
        if not self._storing:
            self._abandoned()
            self._store("interrupted", None, "outcome_unknown")

    def _abandoned(self) -> None:
        logger.warning("Stream abandoned, trace_id=%s, answer_id=%s", self._trace_id, self._started.answer_id)

    def _read(self) -> Generator[str | Message]:
        limit = ANSWER_TIME_LIMIT_SECONDS
        self._called_at = time.monotonic()
        deadline = self._called_at + limit
        arrivals = self._arrivals
        threading.Thread(target=self._receive, args=(arrivals, limit), daemon=True).start()
        pieces: list[str] = []
        text: str | None = None
        # Until the provider ends in time, the outcome is unknown.
        status: str = "interrupted"
        error_code: str | None = "outcome_unknown"
        while True:
            try:
                item, arrived_at = arrivals.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if arrived_at > deadline or isinstance(item, _Left):
                break
            if isinstance(item, str):
                pieces.append(item)
                yield item
            elif isinstance(item, _Ended):
                text = "".join(pieces)
                status, error_code = "completed", None
                if not text.strip():
                    text, status, error_code = None, "failed", "provider_failed"
                break
            elif isinstance(item, ProviderFailed):
                status, error_code = "failed", "provider_failed"
                break
            elif isinstance(item, ProviderTimeout):
                break
            else:
                raise item
        if self._stop.is_set():
            # The reader left while this one waited: whatever the provider sent, nothing is complete.
            self._abandoned()
            text, status, error_code = None, "interrupted", "outcome_unknown"
        self._stop.set()
        yield self._store(status, text, error_code)

    def _receive(self, arrivals: queue.Queue[Arrival], limit: float) -> None:
        """Run on the worker thread: put each piece, then the end or the failure, on the queue."""
        pieces: Iterator[str] | None = None
        try:
            pieces = self._provider.stream(self._started.context, timeout_seconds=limit)
            for piece in pieces:
                arrivals.put((piece, time.monotonic()))
                if self._stop.is_set():
                    return
            arrivals.put((_Ended(), time.monotonic()))
        except Exception as error:
            # The reader decides: a provider failure is stored, anything else is raised there.
            arrivals.put((error, time.monotonic()))
        finally:
            # Ask a provider that is still sending to stop.
            close = getattr(pieces, "close", None)
            if close is not None:
                close()

    def _store(self, status: str, text: str | None, error_code: str | None) -> Message:
        self._storing = True
        with self._open_session() as session:
            return _store_outcome(
                session,
                self._started,
                status,
                text,
                error_code,
                called_at=self._called_at,
                trace_id=self._trace_id,
            )
