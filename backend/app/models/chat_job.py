"""Persistent member chat queue tables for docs/04-reference/chat-policy.md (#52).

`chat_queue_members` holds one row per member that every queue transaction locks
first, so admissions, starts, finishes, cancels and expiries of one member run one
at a time across tabs, devices, conversations and server processes. That lock is the
member's execution right; the partial unique index `chat_jobs_one_running_uq` keeps
it even if a caller skips the lock.

`chat_jobs` is one row per question or manual retry. A running job carries the
`run_token` of the execution that started it; an outcome is stored only together
with that token, so a late result of an earlier execution cannot change the job
after recovery, cancel or a new attempt. `chat_conversation_holds` marks the
conversations whose latest executed question failed; their new questions wait as
`paused` until a retry succeeds or the failure is skipped.

`reservation_id` is the member quota reservation of #15 and has no foreign key here,
so this schema does not depend on the quota tables; #53 can add it when both merge.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JOB_KINDS = ("question", "retry")
WAITING_STATES = ("queued", "paused")
ACTIVE_STATES = (*WAITING_STATES, "running")
RAN_STATES = ("completed", "failed", "interrupted")
NOT_RUN_STATES = ("canceled", "expired")
JOB_STATES = (*ACTIVE_STATES, *RAN_STATES, *NOT_RUN_STATES)

JOB_SHAPE = """
((kind = 'question' AND retry_key IS NULL)
  OR (kind = 'retry' AND retry_key IS NOT NULL AND length(retry_key) > 0))
AND expires_at = accepted_at + interval '5 minutes'
AND (skipped_at IS NULL OR state IN ('failed', 'interrupted'))
AND (
  (state = 'queued' AND paused_by_message_id IS NULL AND started_at IS NULL AND deadline_at IS NULL
    AND run_token IS NULL AND answer_message_id IS NULL AND finished_at IS NULL)
  OR (state = 'paused' AND kind = 'question' AND paused_by_message_id IS NOT NULL AND started_at IS NULL
    AND deadline_at IS NULL AND run_token IS NULL AND answer_message_id IS NULL AND finished_at IS NULL)
  OR (state = 'running' AND paused_by_message_id IS NULL AND started_at IS NOT NULL
    AND deadline_at = started_at + interval '30 seconds' AND run_token IS NOT NULL
    AND answer_message_id IS NOT NULL AND finished_at IS NULL)
  OR (state IN ('completed', 'failed', 'interrupted') AND paused_by_message_id IS NULL AND started_at IS NOT NULL
    AND deadline_at IS NOT NULL AND run_token IS NOT NULL AND answer_message_id IS NOT NULL
    AND finished_at IS NOT NULL)
  OR (state IN ('canceled', 'expired') AND paused_by_message_id IS NULL AND started_at IS NULL
    AND deadline_at IS NULL AND run_token IS NULL AND answer_message_id IS NULL AND finished_at IS NOT NULL)
)
"""


def _states(states: tuple[str, ...]) -> str:
    return ", ".join(f"'{state}'" for state in states)


class ChatQueueMember(Base):
    __tablename__ = "chat_queue_members"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="chat_queue_members_user_fk", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatJob(Base):
    __tablename__ = "chat_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["question_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="chat_jobs_question_fk",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["answer_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="chat_jobs_answer_fk",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"kind IN ({_states(JOB_KINDS)})", name="chat_jobs_kind_ck"),
        CheckConstraint(f"state IN ({_states(JOB_STATES)})", name="chat_jobs_state_ck"),
        CheckConstraint(JOB_SHAPE, name="chat_jobs_shape_ck"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_queue_members.user_id", name="chat_jobs_member_fk", ondelete="RESTRICT")
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", name="chat_jobs_conversation_fk", ondelete="RESTRICT")
    )
    question_message_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    retry_key: Mapped[str | None] = mapped_column(Text)
    # The member quota reservation of #15, resolved exactly once with the job's end.
    reservation_id: Mapped[int | None] = mapped_column(BigInteger)
    paused_by_message_id: Mapped[int | None] = mapped_column(BigInteger)
    answer_message_id: Mapped[int | None] = mapped_column(BigInteger)
    run_token: Mapped[UUID | None] = mapped_column(Uuid)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    skipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationHold(Base):
    """A conversation whose latest executed question failed and was neither retried successfully nor skipped."""

    __tablename__ = "chat_conversation_holds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["question_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="chat_conversation_holds_question_fk",
            ondelete="RESTRICT",
        ),
    )

    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("conversations.id", name="chat_conversation_holds_conversation_fk", ondelete="RESTRICT"),
        primary_key=True,
    )
    question_message_id: Mapped[int] = mapped_column(BigInteger)
    # The latest failed or interrupted job of that question.
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_jobs.id", name="chat_conversation_holds_job_fk", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


Index(
    "chat_jobs_one_running_uq",
    ChatJob.user_id,
    unique=True,
    postgresql_where=text("state = 'running'"),
)
Index(
    "chat_jobs_one_active_per_question_uq",
    ChatJob.question_message_id,
    unique=True,
    postgresql_where=text(f"state IN ({_states(ACTIVE_STATES)})"),
)
Index(
    "chat_jobs_question_once_uq",
    ChatJob.question_message_id,
    unique=True,
    postgresql_where=text("kind = 'question'"),
)
Index(
    "chat_jobs_retry_key_uq",
    ChatJob.question_message_id,
    ChatJob.retry_key,
    unique=True,
    postgresql_where=text("kind = 'retry'"),
)
Index(
    "chat_jobs_answer_uq",
    ChatJob.answer_message_id,
    unique=True,
    postgresql_where=text("answer_message_id IS NOT NULL"),
)
Index(
    "chat_jobs_waiting_idx",
    ChatJob.user_id,
    ChatJob.id,
    postgresql_where=text(f"state IN ({_states(WAITING_STATES)})"),
)
Index(
    "chat_jobs_waiting_expiry_idx", ChatJob.expires_at, postgresql_where=text(f"state IN ({_states(WAITING_STATES)})")
)
Index("chat_jobs_running_deadline_idx", ChatJob.deadline_at, postgresql_where=text("state = 'running'"))
Index("chat_jobs_conversation_idx", ChatJob.conversation_id, ChatJob.id)
