"""Store a question, ask the provider for its answer and store the outcome.

The question and its pending answer are committed before the provider is called,
and the outcome is saved in a second transaction, so no transaction stays open
during the call. A pending answer that is never finished, for example after a
crash between the two, is closed by the next send to the same conversation.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.api.errors import conversation_busy, not_found, request_key_conflict
from app.integrations.provider import AnswerProvider, ContextMessage, ProviderFailed, ProviderTimeout
from app.models.conversation import Message
from app.repositories import conversations as repository

logger = logging.getLogger(__name__)

# Provisional until the PM decides the generation and recovery limits of issue #11.
# A pending answer must stay open longer than the provider is allowed to take.
ANSWER_TIME_LIMIT_SECONDS = 30
STALE_PENDING_SECONDS = 60


@dataclass(frozen=True)
class Exchange:
    question: Message
    answer: Message
    # False when the request key was already stored and no provider call was made.
    created: bool


def send_question(
    session: Session, provider: AnswerProvider, user_id: int, conversation_id: int, content: str, request_key: str
) -> Exchange:
    if not repository.lock_conversation(session, user_id, conversation_id):
        raise not_found()
    repository.close_stale_pending(session, conversation_id, STALE_PENDING_SECONDS)

    question = repository.find_question(session, conversation_id, request_key)
    if question is not None:
        if question.content != content:
            raise request_key_conflict()
        answer = repository.latest_answer(session, question.id)
        # Keeps a stale answer closed.
        session.commit()
        return Exchange(question, answer, created=False)
    if repository.has_pending_answer(session, conversation_id):
        raise conversation_busy()

    stored = repository.list_context_messages(session, user_id, conversation_id) or []
    context = [ContextMessage(message.role, message.content or "") for message in stored]
    context.append(ContextMessage("user", content))
    question, answer = repository.add_question(session, conversation_id, content, request_key)
    # Read before the commit: a session that expires rows on commit would reload them
    # and so reopen a transaction for the duration of the provider call.
    question_id, answer_id = question.id, answer.id
    session.commit()
    logger.info("Question %s saved with pending answer %s", question_id, answer_id)

    text = None
    status, error_code = "completed", None
    try:
        text = provider.complete(context, timeout_seconds=ANSWER_TIME_LIMIT_SECONDS)
    except ProviderFailed:
        status, error_code = "failed", "provider_failed"
    except ProviderTimeout:
        status, error_code = "interrupted", "outcome_unknown"
    else:
        if not text.strip():
            text, status, error_code = None, "failed", "provider_failed"

    answer = repository.finish_answer(session, answer_id, status=status, content=text, error_code=error_code)
    stored_status = answer.status
    session.commit()
    logger.info("Answer %s ended as %s", answer_id, stored_status)
    return Exchange(question, answer, created=True)
