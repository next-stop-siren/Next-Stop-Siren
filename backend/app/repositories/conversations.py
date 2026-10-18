"""Owner-limited conversation storage that the conversation API calls.

`user_id` is always the ID the server verified, never one sent by the screen.
Messages have no user column, so every message query resolves the owner
through the conversation.
"""

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import ColumnElement, Select, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from app.models.conversation import Conversation, Message


def create_conversation(session: Session, user_id: int) -> Conversation:
    """Add a conversation owned by the user and load its generated ID and times.

    The caller commits.
    """
    conversation = Conversation(user_id=user_id)
    session.add(conversation)
    session.flush()
    return conversation


def list_conversations(session: Session, user_id: int, *, limit: int, before_id: int | None) -> Sequence[Conversation]:
    """Return at most `limit` of the user's conversations, newest ID first, below `before_id`."""
    statement = select(Conversation).where(Conversation.user_id == user_id)
    if before_id is not None:
        statement = statement.where(Conversation.id < before_id)
    return session.scalars(statement.order_by(Conversation.id.desc()).limit(limit)).all()


def owns_conversation(session: Session, user_id: int, conversation_id: int) -> bool:
    """False for a missing conversation and for another user's; the two are not distinguishable."""
    statement = select(Conversation.id).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    return session.scalar(statement) is not None


def _owned_messages(user_id: int, conversation_id: int) -> Select[tuple[Message]]:
    return (
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .order_by(Message.id)
    )


def list_messages(
    session: Session, user_id: int, conversation_id: int, *, limit: int, after_id: int | None
) -> Sequence[Message] | None:
    """Return at most `limit` messages in ascending ID order, above `after_id`.

    None means the conversation does not exist or belongs to another user; the two
    cases must not be distinguishable. An owned conversation without messages
    returns an empty sequence.
    """
    if not owns_conversation(session, user_id, conversation_id):
        return None
    statement = _owned_messages(user_id, conversation_id)
    if after_id is not None:
        statement = statement.where(Message.id > after_id)
    return session.scalars(statement.limit(limit)).all()


def list_context_messages(session: Session, user_id: int, conversation_id: int) -> Sequence[Message] | None:
    """Return the completed question+answer pairs in ascending ID order as the next AI context.

    A question whose answer is failed, interrupted or pending is left out together
    with that answer (S11). The rows themselves stay in the history. None has the
    same meaning as in `list_messages`.
    """
    if not owns_conversation(session, user_id, conversation_id):
        return None
    answer = aliased(Message)
    answered = select(answer.reply_to_message_id).where(
        answer.conversation_id == conversation_id, answer.role == "assistant", answer.status == "completed"
    )
    statement = _owned_messages(user_id, conversation_id).where(
        Message.status == "completed",
        or_(Message.role == "assistant", Message.id.in_(answered)),
    )
    return session.scalars(statement).all()


def lock_conversation(session: Session, user_id: int, conversation_id: int) -> bool:
    """Lock the user's conversation row until the transaction ends; False as in `owns_conversation`.

    Sends to one conversation then run one at a time, so the request key and pending
    checks that follow cannot be overtaken by another send. Every transaction that
    changes a conversation's messages takes this lock before any message lock, so two
    of them never wait for each other in opposite order.
    """
    statement = (
        select(Conversation.id)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .with_for_update()
    )
    return session.scalar(statement) is not None


def _pending_answers(conversation_id: int) -> tuple[ColumnElement[bool], ...]:
    return (Message.conversation_id == conversation_id, Message.role == "assistant", Message.status == "pending")


def close_stale_pending(session: Session, conversation_id: int, older_than_seconds: int) -> int:
    """Interrupt pending answers created more than the limit ago, by the database clock; return how many.

    An answer that is no longer pending, or exactly at or within the limit, is left as
    it is. The caller has locked the conversation with `lock_conversation`.
    """
    statement = (
        update(Message)
        .where(*_pending_answers(conversation_id))
        .where(Message.created_at < func.now() - timedelta(seconds=older_than_seconds))
        .values(status="interrupted", error_code="outcome_unknown", updated_at=func.now())
        .returning(Message.id)
        .execution_options(synchronize_session="fetch")
    )
    return len(session.scalars(statement).all())


def has_pending_answer(session: Session, conversation_id: int) -> bool:
    return session.scalar(select(Message.id).where(*_pending_answers(conversation_id))) is not None


def find_question(session: Session, conversation_id: int, request_key: str) -> Message | None:
    statement = select(Message).where(
        Message.conversation_id == conversation_id, Message.role == "user", Message.request_key == request_key
    )
    return session.scalars(statement).one_or_none()


def find_question_by_id(session: Session, conversation_id: int, question_id: int) -> Message | None:
    """None when the ID is not a question of this conversation, such as an answer or another conversation's row."""
    statement = select(Message).where(
        Message.id == question_id, Message.conversation_id == conversation_id, Message.role == "user"
    )
    return session.scalars(statement).one_or_none()


def find_retry(session: Session, question_id: int, retry_key: str) -> Message | None:
    """The answer attempt that this retry key already made for the question."""
    statement = select(Message).where(
        Message.reply_to_message_id == question_id, Message.role == "assistant", Message.retry_key == retry_key
    )
    return session.scalars(statement).one_or_none()


def has_later_question(session: Session, conversation_id: int, question_id: int) -> bool:
    statement = select(Message.id).where(
        Message.conversation_id == conversation_id, Message.role == "user", Message.id > question_id
    )
    return session.scalar(statement.limit(1)) is not None


def latest_answer(session: Session, question_id: int) -> Message:
    """The newest answer attempt; every question is stored together with its first one."""
    statement = (
        select(Message)
        .where(Message.reply_to_message_id == question_id, Message.role == "assistant")
        .order_by(Message.attempt_no.desc())
        .limit(1)
    )
    return session.scalars(statement).one()


def _touch_conversation(session: Session, conversation_id: int) -> None:
    session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


def add_question(session: Session, conversation_id: int, content: str, request_key: str) -> tuple[Message, Message]:
    """Add a question and its first, pending answer.

    The caller commits both together and has already checked the owner, the request
    key and that no other answer is pending.
    """
    question = Message(
        conversation_id=conversation_id,
        role="user",
        status="completed",
        content=content,
        request_key=request_key,
        completed_at=func.now(),
    )
    session.add(question)
    session.flush()
    answer = Message(
        conversation_id=conversation_id,
        role="assistant",
        status="pending",
        reply_to_message_id=question.id,
        attempt_no=1,
    )
    session.add(answer)
    session.flush()
    _touch_conversation(session, conversation_id)
    # Load the times the database generated, so that reading them later needs no query.
    session.refresh(question)
    session.refresh(answer)
    return question, answer


def add_retry(session: Session, conversation_id: int, question_id: int, attempt_no: int, retry_key: str) -> Message:
    """Add a pending answer attempt after the question's latest one.

    The caller commits and has already checked the owner, the retry key, that no
    answer is pending and that the latest attempt may be retried.
    """
    answer = Message(
        conversation_id=conversation_id,
        role="assistant",
        status="pending",
        reply_to_message_id=question_id,
        attempt_no=attempt_no,
        retry_key=retry_key,
    )
    session.add(answer)
    session.flush()
    _touch_conversation(session, conversation_id)
    session.refresh(answer)
    return answer


def finish_answer(
    session: Session, conversation_id: int, answer_id: int, *, status: str, content: str | None, error_code: str | None
) -> Message:
    """Store the outcome only while the answer is still pending; return the answer as stored.

    The conversation row is locked first, in the same order as `lock_conversation`
    followed by `close_stale_pending`. An answer that recovery already closed keeps
    what recovery stored. The caller commits.
    """
    session.execute(select(Conversation.id).where(Conversation.id == conversation_id).with_for_update())
    statement = (
        update(Message)
        .where(Message.id == answer_id, Message.status == "pending")
        .values(
            status=status,
            content=content,
            error_code=error_code,
            updated_at=func.now(),
            completed_at=func.now() if status == "completed" else None,
        )
        .returning(Message)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    answer = session.scalars(statement).one_or_none()
    if answer is None:
        return session.scalars(
            select(Message).where(Message.id == answer_id).execution_options(populate_existing=True)
        ).one()
    _touch_conversation(session, conversation_id)
    return answer
