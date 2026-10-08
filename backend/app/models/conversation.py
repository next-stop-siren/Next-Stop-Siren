"""Conversation ownership and question/answer attempt tables.

Constraint names and conditions follow docs/04-reference/database-reference.sql.
The database guarantees shape, uniqueness and same-conversation replies only;
services still check that a reply targets a user question and that the requester
owns the conversation.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

MESSAGE_SHAPE = """
(role = 'user' AND status = 'completed' AND content IS NOT NULL AND length(btrim(content)) > 0
  AND request_key IS NOT NULL AND length(request_key) > 0 AND reply_to_message_id IS NULL
  AND attempt_no IS NULL AND retry_key IS NULL AND error_code IS NULL AND completed_at IS NOT NULL)
OR
(role = 'assistant' AND reply_to_message_id IS NOT NULL AND attempt_no IS NOT NULL AND attempt_no >= 1
  AND request_key IS NULL AND ((attempt_no = 1 AND retry_key IS NULL)
                         OR (attempt_no > 1 AND retry_key IS NOT NULL AND length(retry_key) > 0))
  AND ((status = 'completed' AND content IS NOT NULL AND length(content) > 0
        AND completed_at IS NOT NULL AND error_code IS NULL)
    OR (status = 'pending' AND content IS NULL AND completed_at IS NULL AND error_code IS NULL)
    OR (status IN ('failed', 'interrupted') AND content IS NULL AND completed_at IS NULL
        AND error_code IS NOT NULL)))
"""


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="conversations_user_fk", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Target of the composite reply key below.
        UniqueConstraint("id", "conversation_id", name="messages_id_conversation_uq"),
        ForeignKeyConstraint(
            ["reply_to_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="messages_reply_same_conversation_fk",
            ondelete="RESTRICT",
        ),
        CheckConstraint("role IN ('user', 'assistant')", name="messages_role_ck"),
        CheckConstraint("status IN ('pending', 'completed', 'failed', 'interrupted')", name="messages_status_ck"),
        CheckConstraint(MESSAGE_SHAPE, name="messages_shape_ck"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", name="messages_conversation_fk", ondelete="RESTRICT")
    )
    role: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    request_key: Mapped[str | None] = mapped_column(Text)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    attempt_no: Mapped[int | None] = mapped_column(Integer)
    retry_key: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "conversations_owner_list_idx",
    Conversation.user_id,
    Conversation.updated_at.desc(),
    Conversation.id.desc(),
)
Index(
    "messages_question_key_uq",
    Message.conversation_id,
    Message.request_key,
    unique=True,
    postgresql_where=text("role = 'user'"),
)
Index(
    "messages_attempt_uq",
    Message.reply_to_message_id,
    Message.attempt_no,
    unique=True,
    postgresql_where=text("role = 'assistant'"),
)
Index(
    "messages_retry_key_uq",
    Message.reply_to_message_id,
    Message.retry_key,
    unique=True,
    postgresql_where=text("role = 'assistant' AND retry_key IS NOT NULL"),
)
Index(
    "messages_one_pending_uq",
    Message.conversation_id,
    unique=True,
    postgresql_where=text("role = 'assistant' AND status = 'pending'"),
)
Index("messages_order_idx", Message.conversation_id, Message.id)
Index("messages_reply_idx", Message.reply_to_message_id, Message.attempt_no.desc())
