"""Store a question, ask the provider for its answer and store the outcome.

The question and its pending answer are committed before the provider is called,
and the outcome is saved in a second transaction, so no transaction stays open
during the call. A pending answer that is never finished, for example after a
crash between the two, is closed by the next send to the same conversation.
A streamed answer follows the same two transactions; its pieces are never stored.

Refusals are raised as the business errors below; the API layer turns them into
HTTP responses. Log entries carry the caller's trace ID and row IDs only, never a
question, a request key, an answer or a provider's error message.
"""

import logging
import time
from collections.abc import Callable, Generator
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


def stream_answer(
    open_session: Callable[[], AbstractContextManager[Session]],
    provider: AnswerProvider,
    started: Started,
    *,
    trace_id: str,
) -> Generator[str | Message]:
    """Yield the pieces of the answer as they arrive, then the answer as stored.

    A piece is temporary output. Nothing is stored until the provider has ended, and
    the answer is yielded only after its commit, so a reader that has not received it
    cannot take the answer as complete. A failure or time limit between two pieces
    stores no text. Closing the generator before the provider has ended, as when the
    reader has left, discards the pieces and stores the answer as interrupted.
    `open_session` gives the session of that one short transaction.
    """
    pieces: list[str] = []
    text: str | None = None
    status, error_code = "completed", None
    called_at = time.monotonic()
    try:
        for piece in provider.stream(started.context, timeout_seconds=ANSWER_TIME_LIMIT_SECONDS):
            pieces.append(piece)
            yield piece
    except ProviderFailed:
        status, error_code = "failed", "provider_failed"
    except ProviderTimeout:
        status, error_code = "interrupted", "outcome_unknown"
    except GeneratorExit:
        logger.warning("Stream abandoned, trace_id=%s, answer_id=%s", trace_id, started.answer_id)
        with open_session() as session:
            _store_outcome(
                session, started, "interrupted", None, "outcome_unknown", called_at=called_at, trace_id=trace_id
            )
        raise
    else:
        text = "".join(pieces)
        if not text.strip():
            text, status, error_code = None, "failed", "provider_failed"
    with open_session() as session:
        answer = _store_outcome(session, started, status, text, error_code, called_at=called_at, trace_id=trace_id)
    yield answer
