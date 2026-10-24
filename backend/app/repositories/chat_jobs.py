"""Persistent chat job storage that the chat queue service calls (#52).

`user_id` is always the ID the server verified. Nothing here commits. Every queue
transaction calls `lock_member` before it locks a conversation or a job, so two of
them never wait for each other in opposite order.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from app.models.chat_job import ACTIVE_STATES, WAITING_STATES, ChatJob, ChatQueueMember, ConversationHold
from app.models.conversation import Conversation, Message


def database_time(session: Session) -> datetime:
    """The database clock now, not the transaction start, so a wait for a lock is not counted as earlier."""
    return session.scalars(select(func.clock_timestamp())).one()


def lock_member(session: Session, user_id: int) -> None:
    """Create the member's queue row if needed and lock it until the transaction ends."""
    session.execute(insert(ChatQueueMember).values(user_id=user_id).on_conflict_do_nothing(index_elements=["user_id"]))
    session.execute(select(ChatQueueMember.user_id).where(ChatQueueMember.user_id == user_id).with_for_update())


def job_owner(session: Session, job_id: int) -> int | None:
    """The member of a job, read without a lock so that the member lock can be taken first."""
    return session.scalar(select(ChatJob.user_id).where(ChatJob.id == job_id))


def lock_job(session: Session, job_id: int, *, user_id: int | None = None) -> ChatJob | None:
    """Lock and reload the job; with `user_id`, None also for another member's job."""
    statement = select(ChatJob).where(ChatJob.id == job_id)
    if user_id is not None:
        statement = statement.where(ChatJob.user_id == user_id)
    return session.scalars(statement.with_for_update().execution_options(populate_existing=True)).one_or_none()


def lock_owned_conversation(session: Session, user_id: int, conversation_id: int) -> bool:
    """Lock the member's conversation; False for a missing one and for another member's."""
    statement = (
        select(Conversation.id)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .with_for_update()
    )
    return session.scalar(statement) is not None


def waiting_counts(session: Session, user_id: int) -> tuple[int, int, int]:
    """Running jobs, waiting new questions (`queued` or `paused`) and queued retries of the member."""
    statement = (
        select(ChatJob.kind, ChatJob.state, func.count())
        .where(ChatJob.user_id == user_id, ChatJob.state.in_(ACTIVE_STATES))
        .group_by(ChatJob.kind, ChatJob.state)
    )
    running = questions = retries = 0
    for kind, state, count in session.execute(statement).tuples():
        if state == "running":
            running += count
        elif kind == "question":
            questions += count
        else:
            retries += count
    return running, questions, retries


def find_question(session: Session, conversation_id: int, request_key: str) -> Message | None:
    statement = select(Message).where(
        Message.conversation_id == conversation_id, Message.role == "user", Message.request_key == request_key
    )
    return session.scalars(statement).one_or_none()


def find_question_by_id(session: Session, conversation_id: int, question_id: int) -> Message | None:
    statement = select(Message).where(
        Message.id == question_id, Message.conversation_id == conversation_id, Message.role == "user"
    )
    return session.scalars(statement).one_or_none()


def question_job(session: Session, question_id: int) -> ChatJob | None:
    return session.scalars(
        select(ChatJob).where(ChatJob.question_message_id == question_id, ChatJob.kind == "question")
    ).one_or_none()


def retry_job(session: Session, question_id: int, retry_key: str) -> ChatJob | None:
    statement = select(ChatJob).where(
        ChatJob.question_message_id == question_id, ChatJob.kind == "retry", ChatJob.retry_key == retry_key
    )
    return session.scalars(statement).one_or_none()


def retry_answer(session: Session, question_id: int, retry_key: str) -> Message | None:
    """The answer attempt that this retry key made for the question, with or without a job.

    The synchronous API before #53 stored retries as answer rows only, so a resend of
    such a key has no job to find. `messages_retry_key_uq` keeps one row per key.
    """
    statement = select(Message).where(
        Message.reply_to_message_id == question_id, Message.role == "assistant", Message.retry_key == retry_key
    )
    return session.scalars(statement.execution_options(populate_existing=True)).one_or_none()


def has_active_job(session: Session, question_id: int) -> bool:
    statement = select(ChatJob.id).where(ChatJob.question_message_id == question_id, ChatJob.state.in_(ACTIVE_STATES))
    return session.scalar(statement) is not None


def latest_answer(session: Session, question_id: int) -> Message | None:
    """The newest answer attempt; None before the question's first execution."""
    statement = (
        select(Message)
        .where(Message.reply_to_message_id == question_id, Message.role == "assistant")
        .order_by(Message.attempt_no.desc())
        .limit(1)
    )
    return session.scalars(statement).one_or_none()


def later_question_started(session: Session, conversation_id: int, question_id: int) -> bool:
    """Whether a later question of the conversation has an answer row, which exists only once it started."""
    statement = select(Message.id).where(
        Message.conversation_id == conversation_id,
        Message.role == "assistant",
        Message.reply_to_message_id > question_id,
    )
    return session.scalar(statement.limit(1)) is not None


def hold(session: Session, conversation_id: int) -> ConversationHold | None:
    return session.get(ConversationHold, conversation_id, populate_existing=True)


def _touch_conversation(session: Session, conversation_id: int) -> None:
    session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


def add_question(session: Session, conversation_id: int, content: str, request_key: str) -> Message:
    """Add a question without an answer; the answer row is added when its job starts."""
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
    _touch_conversation(session, conversation_id)
    session.refresh(question)
    return question


def add_job(
    session: Session,
    *,
    user_id: int,
    conversation_id: int,
    question_id: int,
    kind: str,
    retry_key: str | None,
    reservation_id: int | None,
    paused_by_message_id: int | None,
    accepted_at: datetime,
    wait_limit: timedelta,
) -> ChatJob:
    job = ChatJob(
        user_id=user_id,
        conversation_id=conversation_id,
        question_message_id=question_id,
        kind=kind,
        state="queued" if paused_by_message_id is None else "paused",
        retry_key=retry_key,
        reservation_id=reservation_id,
        paused_by_message_id=paused_by_message_id,
        accepted_at=accepted_at,
        expires_at=accepted_at + wait_limit,
    )
    session.add(job)
    session.flush()
    return job


def running_job(session: Session, user_id: int) -> ChatJob | None:
    statement = (
        select(ChatJob)
        .where(ChatJob.user_id == user_id, ChatJob.state == "running")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalars(statement).one_or_none()


def next_runnable(session: Session, user_id: int, now: datetime) -> ChatJob | None:
    """The member's oldest queued job that has not expired; a queued retry goes before its conversation's questions.

    A paused job is never runnable. Jobs of other conversations keep their order, so a
    retry overtakes only the later questions of its own conversation.
    """
    retry = aliased(ChatJob)
    queued_retry = exists().where(
        retry.conversation_id == ChatJob.conversation_id, retry.state == "queued", retry.kind == "retry"
    )
    statement = (
        select(ChatJob)
        .where(ChatJob.user_id == user_id, ChatJob.state == "queued", ChatJob.expires_at > now)
        .where(or_(ChatJob.kind == "retry", ~queued_retry))
        .order_by(ChatJob.id)
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalars(statement).one_or_none()


def due_waiting(session: Session, user_id: int, now: datetime) -> Sequence[ChatJob]:
    statement = (
        select(ChatJob)
        .where(ChatJob.user_id == user_id, ChatJob.state.in_(WAITING_STATES), ChatJob.expires_at <= now)
        .order_by(ChatJob.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.scalars(statement).all()


def close_orphan_pending(session: Session, conversation_id: int) -> list[int]:
    """Interrupt pending answers of the conversation that no job runs; return their IDs.

    Only the synchronous API before #53 made such answers. One left by a crash would
    otherwise block the conversation's next start, as a conversation has one pending
    answer at most.
    """
    statement = (
        update(Message)
        .where(Message.conversation_id == conversation_id, Message.role == "assistant", Message.status == "pending")
        .where(~exists().where(ChatJob.answer_message_id == Message.id))
        .values(status="interrupted", error_code="outcome_unknown", updated_at=func.now())
        .returning(Message.id)
        .execution_options(synchronize_session=False)
    )
    return list(session.scalars(statement).all())


def add_answer(
    session: Session, conversation_id: int, question_id: int, attempt_no: int, retry_key: str | None
) -> Message:
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
    return answer


def context_before(session: Session, conversation_id: int, question_id: int) -> Sequence[Message]:
    """The completed question+answer pairs before the question, in ascending ID order."""
    answer = aliased(Message)
    answered = select(answer.reply_to_message_id).where(
        answer.conversation_id == conversation_id, answer.role == "assistant", answer.status == "completed"
    )
    statement = (
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.status == "completed")
        .where(or_(Message.role == "assistant", Message.id.in_(answered)))
        .where(or_(Message.role == "user", Message.reply_to_message_id < question_id))
        .where(or_(Message.role == "assistant", Message.id < question_id))
        .order_by(Message.id)
    )
    return session.scalars(statement).all()


def get_message(session: Session, message_id: int) -> Message:
    return session.scalars(
        select(Message).where(Message.id == message_id).execution_options(populate_existing=True)
    ).one()


def pause_questions(session: Session, conversation_id: int, failed_question_id: int) -> list[int]:
    """Pause the conversation's queued new questions behind the failed question; return their IDs."""
    statement = (
        update(ChatJob)
        .where(ChatJob.conversation_id == conversation_id, ChatJob.state == "queued", ChatJob.kind == "question")
        .values(state="paused", paused_by_message_id=failed_question_id)
        .returning(ChatJob.id)
        .execution_options(synchronize_session=False)
    )
    return list(session.scalars(statement).all())


def resume_questions(session: Session, conversation_id: int) -> list[int]:
    statement = (
        update(ChatJob)
        .where(ChatJob.conversation_id == conversation_id, ChatJob.state == "paused")
        .values(state="queued", paused_by_message_id=None)
        .returning(ChatJob.id)
        .execution_options(synchronize_session=False)
    )
    return list(session.scalars(statement).all())


def put_hold(session: Session, conversation_id: int, question_id: int, job_id: int, now: datetime) -> None:
    statement = insert(ConversationHold).values(
        conversation_id=conversation_id, question_message_id=question_id, job_id=job_id, created_at=now
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["conversation_id"],
            set_={"question_message_id": question_id, "job_id": job_id, "created_at": now},
        )
    )


def delete_hold(session: Session, conversation_id: int) -> None:
    held = session.get(ConversationHold, conversation_id)
    if held is not None:
        session.delete(held)
        session.flush()


def owned_job(session: Session, user_id: int, job_id: int) -> ChatJob | None:
    """The member's job read without a lock; None also for another member's."""
    statement = select(ChatJob).where(ChatJob.id == job_id, ChatJob.user_id == user_id)
    return session.scalars(statement.execution_options(populate_existing=True)).one_or_none()


def owned_jobs(session: Session, user_id: int, job_ids: Sequence[int]) -> Sequence[ChatJob]:
    statement = select(ChatJob).where(ChatJob.user_id == user_id, ChatJob.id.in_(job_ids)).order_by(ChatJob.id)
    return session.scalars(statement.execution_options(populate_existing=True)).all()


def active_jobs(session: Session, user_id: int) -> Sequence[ChatJob]:
    """The member's jobs that have not ended, oldest first."""
    statement = select(ChatJob).where(ChatJob.user_id == user_id, ChatJob.state.in_(ACTIVE_STATES)).order_by(ChatJob.id)
    return session.scalars(statement.execution_options(populate_existing=True)).all()


def queued_jobs(session: Session, user_id: int) -> Sequence[ChatJob]:
    statement = select(ChatJob).where(ChatJob.user_id == user_id, ChatJob.state == "queued").order_by(ChatJob.id)
    return session.scalars(statement).all()


def conversation_jobs(
    session: Session, conversation_id: int, *, limit: int, before_id: int | None
) -> Sequence[ChatJob]:
    """At most `limit` jobs of the conversation, newest ID first, below `before_id`."""
    statement = select(ChatJob).where(ChatJob.conversation_id == conversation_id)
    if before_id is not None:
        statement = statement.where(ChatJob.id < before_id)
    return session.scalars(statement.order_by(ChatJob.id.desc()).limit(limit)).all()


def ended_since(session: Session, user_id: int, since: datetime) -> Sequence[ChatJob]:
    statement = select(ChatJob).where(ChatJob.user_id == user_id, ChatJob.finished_at >= since).order_by(ChatJob.id)
    return session.scalars(statement).all()


def touched_conversations(session: Session, user_id: int, since: datetime) -> Sequence[int]:
    """Conversations where a job was admitted, started, ended or skipped at or after `since`.

    Every change of an `allowed_actions` is caused by one of these in the same conversation.
    """
    statement = (
        select(ChatJob.conversation_id)
        .where(ChatJob.user_id == user_id)
        .where(
            or_(
                ChatJob.accepted_at >= since,
                ChatJob.started_at >= since,
                ChatJob.finished_at >= since,
                ChatJob.skipped_at >= since,
            )
        )
        .distinct()
    )
    return session.scalars(statement).all()


def failed_recent_jobs(session: Session, conversation_ids: Sequence[int]) -> Sequence[ChatJob]:
    """Every failed or interrupted attempt of the two last executed questions of each conversation.

    These are the jobs whose retry and skip buttons can change: a new start in a
    conversation affects its latest executed question and the one before it. Earlier
    attempts are included too, because a retry that completes elsewhere takes the
    buttons off the attempt before it; the screen must receive that attempt with `[]`.
    """
    if not conversation_ids:
        return []
    answer = aliased(Message)
    recent = (
        select(answer.reply_to_message_id)
        .where(answer.conversation_id == ChatJob.conversation_id, answer.role == "assistant")
        .group_by(answer.reply_to_message_id)
        .order_by(func.max(answer.id).desc())
        .limit(2)
        .correlate(ChatJob)
    )
    statement = (
        select(ChatJob)
        .where(ChatJob.conversation_id.in_(conversation_ids), ChatJob.state.in_(("failed", "interrupted")))
        .where(ChatJob.question_message_id.in_(recent))
        .order_by(ChatJob.id)
    )
    return session.scalars(statement.execution_options(populate_existing=True)).all()


def members_with_work(session: Session, now: datetime, stale_before: datetime) -> Sequence[int]:
    """Members with a queued job, a wait that has expired or a running job whose time is over."""
    statement = (
        select(ChatJob.user_id)
        .where(
            or_(
                ChatJob.state == "queued",
                (ChatJob.state.in_(WAITING_STATES)) & (ChatJob.expires_at <= now),
                (ChatJob.state == "running") & (ChatJob.deadline_at <= stale_before),
            )
        )
        .distinct()
        .order_by(ChatJob.user_id)
    )
    return session.scalars(statement).all()
