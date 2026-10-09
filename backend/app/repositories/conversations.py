"""Owner-limited conversation storage that the conversation API calls.

`user_id` is always the ID the server verified, never one sent by the screen.
Messages have no user column, so every message query resolves the owner
through the conversation.
"""

from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

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
    """Return every completed message in ascending ID order as the next AI context.

    Failed, interrupted and pending answers are left out. Whether a question whose
    answer failed stays in the context is an open PM decision; it stays for now.
    None has the same meaning as in `list_messages`.
    """
    if not owns_conversation(session, user_id, conversation_id):
        return None
    statement = _owned_messages(user_id, conversation_id).where(Message.status == "completed")
    return session.scalars(statement).all()
