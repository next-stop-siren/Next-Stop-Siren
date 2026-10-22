"""Owner-limited conversation storage that the conversation API calls.

`user_id` is always the ID the server verified, never one sent by the screen.
Messages have no user column, so every message query resolves the owner
through the conversation.
"""

from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select, update
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


def _touch_conversation(session: Session, conversation_id: int) -> None:
    session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


def finish_answer(
    session: Session, conversation_id: int, answer_id: int, *, status: str, content: str | None, error_code: str | None
) -> Message:
    """Store the outcome only while the answer is still pending; return the answer as stored.

    The conversation row is locked before the message, the chat queue's lock order
    after the member row. An answer that recovery already closed keeps what recovery
    stored. The caller commits.
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
