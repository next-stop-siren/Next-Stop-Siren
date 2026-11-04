"""Persistent member chat queue: admission, the execution right, outcomes and recovery (#52).

The approved target is docs/04-reference/chat-policy.md; job states, error codes and
the order of the admission checks follow docs/04-reference/api.md. The conversation
and chat APIs call this module with the #15 quota as the ledger (#53).

Every function here runs in one short transaction that the caller commits, and the
first lock it takes is the member's queue row. Admissions, starts, finishes, cancels,
expiries and skips of one member are therefore applied one at a time, and the
conditions they check cannot change before they commit. No function waits for the
provider or for a subscriber: `claim_next` commits the start, the runner calls the
provider with no transaction open, and `finish` stores the outcome in a new one.

A running job holds the `run_token` of its execution. `finish` stores an outcome only
for the job that is still running under that token, so a result that arrives after
recovery interrupted the job, or after a later attempt started, changes nothing. A
running job is the member's only one; it is left alone until its 30-second deadline
plus `RECOVERY_GRACE_SECONDS` has passed, then `recover` reconciles it with the
stored answer: a completed answer completes the job, anything else interrupts it.
Recovery never calls the provider again.

The member quota (#15) is reached through `QuotaLedger`. Admission reserves after the
queue checks and in the same transaction, and every job end resolves the reservation
once, in the transaction that ends the job: settled when completed, refunded with
the job state otherwise. The ledger enforces the 10-second interval between new
admissions and the daily and six-hour limits, and raises its own refusal.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast

from sqlalchemy.orm import Session

from app.integrations.provider import ContextMessage
from app.models.chat_job import ChatJob
from app.models.conversation import Message
from app.repositories import chat_jobs as repository
from app.repositories import conversations as conversation_repository
from app.services import chat_context
from app.services.chat import (
    ANSWER_TIME_LIMIT_SECONDS,
    RETRYABLE_STATUSES,
    ConversationNotFound,
    NotLatestQuestion,
    QuestionNotFound,
    RequestKeyConflict,
    RetryNotAllowed,
)

logger = logging.getLogger(__name__)

QUESTIONS_MAX = 3
RETRIES_MAX = 1
WAIT_LIMIT = timedelta(minutes=5)
ANSWER_TIME_LIMIT = timedelta(seconds=ANSWER_TIME_LIMIT_SECONDS)
# A running job is reconciled only after its deadline and this margin, so an execution
# that is still storing its outcome is not interrupted under it.
RECOVERY_GRACE_SECONDS = 15

RefundReason = Literal["failed", "interrupted", "canceled", "expired"]


class QuotaLedger(Protocol):
    """The #15 member quota as the queue uses it; nothing here commits.

    `reserve` raises the quota's own refusal (`attempt_too_soon`, `quota_exceeded`)
    and otherwise returns the reservation ID. `settle` and `refund` return False when
    the reservation was already resolved.
    """

    def reserve(self, session: Session, user_id: int, *, now: datetime) -> int: ...

    def settle(self, session: Session, reservation_id: int) -> bool: ...

    def refund(self, session: Session, reservation_id: int, reason: RefundReason) -> bool: ...


class QueueError(Exception):
    """A queue request refused before anything new is stored."""


class QueueFull(QueueError):
    """`queue_full`: the member already has three new questions waiting."""


class RetryQueueFull(QueueError):
    """`retry_queue_full`: the member already has a retry waiting."""


class JobNotFound(QueueError):
    """The job does not exist or belongs to another member; the two are not distinguishable."""


class JobNotCancelable(QueueError):
    """`job_not_cancelable`: the job is running or has already ended other than by cancel."""


class SkipNotAllowed(QueueError):
    """`skip_not_allowed`: the question does not hold its conversation, or its retry is waiting or running."""


@dataclass(frozen=True)
class Admission:
    question: Message
    # None only for a question or retry that the synchronous API before #53 stored, which has no job.
    job: ChatJob | None
    # False when the key was already admitted; nothing was reserved or stored.
    created: bool
    # The attempt that a retry key of the synchronous API made; None otherwise.
    answer: Message | None = None


@dataclass(frozen=True)
class Claimed:
    """A job that started; the provider has not been called yet."""

    job_id: int
    run_token: uuid.UUID
    answer_message_id: int
    context: list[ContextMessage]


@dataclass(frozen=True)
class Outcome:
    """How an execution ended; a completed one always carries its non-blank answer, as `messages` requires."""

    status: Literal["completed", "failed", "interrupted"]
    text: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if (self.status == "completed") != (self.text is not None and bool(self.text.strip())):
            raise ValueError("a completed outcome needs its answer text, and only a completed one has text")


PROVIDER_FAILED = Outcome("failed", error_code="provider_failed")
OUTCOME_UNKNOWN = Outcome("interrupted", error_code="outcome_unknown")


@dataclass
class Changes:
    """Jobs whose state changed in the transaction, for the subscription events of #53 after it commits."""

    job_ids: list[int] = field(default_factory=list)

    def add(self, *job_ids: int) -> None:
        self.job_ids.extend(job_id for job_id in job_ids if job_id not in self.job_ids)


def _now(session: Session, now: datetime | None) -> datetime:
    return repository.database_time(session) if now is None else now


def admit_question(
    session: Session,
    ledger: QuotaLedger,
    user_id: int,
    conversation_id: int,
    content: str,
    request_key: str,
    *,
    now: datetime | None = None,
) -> Admission:
    """Store a new question with its job and quota reservation, or return the one stored for the key.

    A question of a held conversation waits as `paused`. Checks run in the draft's
    order: owner, same key, key conflict, `queue_full`, then the ledger.
    """
    repository.lock_member(session, user_id)
    if not repository.lock_owned_conversation(session, user_id, conversation_id):
        raise ConversationNotFound
    question = repository.find_question(session, conversation_id, request_key)
    if question is not None:
        if question.content != content:
            raise RequestKeyConflict
        return Admission(question, repository.question_job(session, question.id), created=False)
    now = _now(session, now)
    _, questions, _ = repository.waiting_counts(session, user_id)
    if questions >= QUESTIONS_MAX:
        raise QueueFull
    reservation_id = ledger.reserve(session, user_id, now=now)
    held = repository.hold(session, conversation_id)
    question = repository.add_question(session, conversation_id, content, request_key)
    job = repository.add_job(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        question_id=question.id,
        kind="question",
        retry_key=None,
        reservation_id=reservation_id,
        paused_by_message_id=None if held is None else held.question_message_id,
        accepted_at=now,
        wait_limit=WAIT_LIMIT,
    )
    return Admission(question, job, created=True)


def admit_retry(
    session: Session,
    ledger: QuotaLedger,
    user_id: int,
    conversation_id: int,
    question_id: int,
    retry_key: str,
    *,
    now: datetime | None = None,
) -> Admission:
    """Queue a manual retry of the conversation's latest executed question, or return the one stored for the key.

    The question's latest answer must be failed or interrupted and no later question
    may have started; a later question that only waits does not prevent it.

    A key is looked up before any of those checks, first among the queue's jobs, then
    among the attempts that the synchronous API before #53 stored without a job. Either
    way the stored attempt is returned as it is: a resend is never refused for a state
    its own attempt caused, and never admitted again, which would also break the
    attempt's unique retry key when it starts.
    """
    repository.lock_member(session, user_id)
    if not repository.lock_owned_conversation(session, user_id, conversation_id):
        raise ConversationNotFound
    question = repository.find_question_by_id(session, conversation_id, question_id)
    if question is None:
        raise QuestionNotFound
    stored = repository.retry_job(session, question.id, retry_key)
    if stored is not None:
        return Admission(question, stored, created=False)
    earlier = repository.retry_answer(session, question.id, retry_key)
    if earlier is not None:
        return Admission(question, None, created=False, answer=earlier)
    latest = repository.latest_answer(session, question.id)
    if latest is None or latest.status not in RETRYABLE_STATUSES or repository.has_active_job(session, question.id):
        raise RetryNotAllowed
    if repository.later_question_started(session, conversation_id, question.id):
        raise NotLatestQuestion
    now = _now(session, now)
    _, _, retries = repository.waiting_counts(session, user_id)
    if retries >= RETRIES_MAX:
        raise RetryQueueFull
    reservation_id = ledger.reserve(session, user_id, now=now)
    job = repository.add_job(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        question_id=question.id,
        kind="retry",
        retry_key=retry_key,
        reservation_id=reservation_id,
        paused_by_message_id=None,
        accepted_at=now,
        wait_limit=WAIT_LIMIT,
    )
    return Admission(question, job, created=True)


def _resolve(session: Session, ledger: QuotaLedger, job: ChatJob) -> None:
    if job.reservation_id is None:
        return
    if job.state == "completed":
        resolved = ledger.settle(session, job.reservation_id)
    else:
        resolved = ledger.refund(session, job.reservation_id, cast(RefundReason, job.state))
    if not resolved:
        logger.warning("Reservation already resolved, job_id=%s, state=%s", job.id, job.state)


def _end_unstarted(session: Session, ledger: QuotaLedger, job: ChatJob, state: str, now: datetime) -> None:
    job.state = state
    job.paused_by_message_id = None
    job.finished_at = now
    session.flush()
    _resolve(session, ledger, job)


def _expire_due(session: Session, ledger: QuotaLedger, user_id: int, now: datetime, changes: Changes) -> None:
    for job in repository.due_waiting(session, user_id, now):
        _end_unstarted(session, ledger, job, "expired", now)
        changes.add(job.id)


def _conclude(session: Session, ledger: QuotaLedger, job: ChatJob, answer: Message, now: datetime) -> Changes:
    """End a running job with the outcome its answer stored, resolve the quota once and update the hold."""
    changes = Changes()
    # `finish_answer` keeps an answer that is no longer pending as it was stored.
    job.state = answer.status if answer.status in ("completed", "failed", "interrupted") else "interrupted"
    job.finished_at = now
    session.flush()
    _resolve(session, ledger, job)
    changes.add(job.id)
    if job.state == "completed":
        held = repository.hold(session, job.conversation_id)
        if held is not None and held.question_message_id == job.question_message_id:
            failed_job_id = held.job_id
            repository.delete_hold(session, job.conversation_id)
            changes.add(failed_job_id, *repository.resume_questions(session, job.conversation_id))
    else:
        held = repository.hold(session, job.conversation_id)
        if held is not None:
            # The earlier failed attempt no longer offers retry or skip.
            changes.add(held.job_id)
        repository.put_hold(session, job.conversation_id, job.question_message_id, job.id, now)
        changes.add(*repository.pause_questions(session, job.conversation_id, job.question_message_id))
    return changes


def _stale(job: ChatJob, now: datetime) -> bool:
    return job.deadline_at is not None and job.deadline_at + timedelta(seconds=RECOVERY_GRACE_SECONDS) <= now


def _reconcile(session: Session, ledger: QuotaLedger, job: ChatJob, now: datetime) -> Changes:
    """Close a running job whose execution is over: keep a stored completed answer, else interrupt it."""
    assert job.answer_message_id is not None
    answer = repository.get_message(session, job.answer_message_id)
    if answer.status == "pending":
        answer = conversation_repository.finish_answer(
            session,
            job.conversation_id,
            answer.id,
            status=OUTCOME_UNKNOWN.status,
            content=None,
            error_code=OUTCOME_UNKNOWN.error_code,
        )
    logger.warning("Running job reconciled, job_id=%s, answer_status=%s", job.id, answer.status)
    return _conclude(session, ledger, job, answer, now)


def recover(session: Session, ledger: QuotaLedger, user_id: int, *, now: datetime | None = None) -> Changes:
    """Expire the member's waits past five minutes and reconcile a running job past its deadline and grace.

    A running job still inside that time may belong to a live execution and is left alone.
    """
    repository.lock_member(session, user_id)
    now = _now(session, now)
    changes = Changes()
    running = repository.running_job(session, user_id)
    if running is not None and _stale(running, now):
        changes.add(*_reconcile(session, ledger, running, now).job_ids)
    _expire_due(session, ledger, user_id, now, changes)
    return changes


def claim_next(
    session: Session, ledger: QuotaLedger, user_id: int, *, now: datetime | None = None
) -> tuple[Claimed | None, Changes]:
    """Take the member's execution right for the next runnable job and start it, or return None.

    Recovery and expiry run first under the same lock. A started job has a new pending
    answer, a new `run_token` and a deadline 30 seconds after its start; its context
    is read now, at execution, from the completed pairs before its question, in pair
    order and within the budget of `chat_context`.
    """
    changes = recover(session, ledger, user_id, now=now)
    now = _now(session, now)
    if repository.running_job(session, user_id) is not None:
        return None, changes
    job = repository.next_runnable(session, user_id, now)
    if job is None:
        return None, changes
    orphans = repository.close_orphan_pending(session, job.conversation_id)
    if orphans:
        logger.warning("Pending answer without a job interrupted, job_id=%s, count=%s", job.id, len(orphans))
    latest = repository.latest_answer(session, job.question_message_id)
    attempt_no = 1 if latest is None or latest.attempt_no is None else latest.attempt_no + 1
    answer = repository.add_answer(session, job.conversation_id, job.question_message_id, attempt_no, job.retry_key)
    job.state = "running"
    job.started_at = now
    job.deadline_at = now + ANSWER_TIME_LIMIT
    job.run_token = uuid.uuid4()
    job.answer_message_id = answer.id
    session.flush()
    changes.add(job.id)
    question = repository.get_message(session, job.question_message_id)
    pairs = chat_context.pair_messages(repository.context_before(session, job.conversation_id, job.question_message_id))
    built = chat_context.build_context(pairs, question.content or "")
    # Counts only, never the text, so a left-out pair can be traced without its content.
    logger.info(
        "event=chat_context_built job_id=%s pairs_sent=%s pairs_left_out=%s characters=%s",
        job.id,
        built.pairs_sent,
        built.pairs_left_out,
        built.characters,
    )
    return Claimed(job.id, job.run_token, answer.id, built.messages), changes


def finish(
    session: Session,
    ledger: QuotaLedger,
    job_id: int,
    run_token: uuid.UUID,
    outcome: Outcome,
    *,
    now: datetime | None = None,
) -> Changes | None:
    """Store the outcome of the execution `run_token` started; None for a late result that changed nothing."""
    user_id = repository.job_owner(session, job_id)
    if user_id is None:
        return None
    repository.lock_member(session, user_id)
    job = repository.lock_job(session, job_id)
    if job is None or job.state != "running" or job.run_token != run_token:
        logger.warning("Late result ignored, job_id=%s", job_id)
        return None
    now = _now(session, now)
    assert job.answer_message_id is not None
    answer = conversation_repository.finish_answer(
        session,
        job.conversation_id,
        job.answer_message_id,
        status=outcome.status,
        content=outcome.text,
        error_code=outcome.error_code,
    )
    return _conclude(session, ledger, job, answer, now)


def cancel(
    session: Session, ledger: QuotaLedger, user_id: int, job_id: int, *, now: datetime | None = None
) -> tuple[ChatJob, Changes]:
    """Cancel a job that has not started; a job already canceled is returned as it is.

    A job that started, ended or reached its five minutes first is not canceled.
    """
    repository.lock_member(session, user_id)
    now = _now(session, now)
    changes = Changes()
    _expire_due(session, ledger, user_id, now, changes)
    job = repository.lock_job(session, job_id, user_id=user_id)
    if job is None:
        raise JobNotFound
    if job.state == "canceled":
        return job, changes
    if job.state not in ("queued", "paused"):
        raise JobNotCancelable
    _end_unstarted(session, ledger, job, "canceled", now)
    changes.add(job.id)
    return job, changes


def skip(
    session: Session, user_id: int, conversation_id: int, question_id: int, *, now: datetime | None = None
) -> Changes:
    """Leave the conversation's failed question as it is and resume its paused questions; uses no quota."""
    repository.lock_member(session, user_id)
    if not repository.lock_owned_conversation(session, user_id, conversation_id):
        raise ConversationNotFound
    if repository.find_question_by_id(session, conversation_id, question_id) is None:
        raise QuestionNotFound
    now = _now(session, now)
    held = repository.hold(session, conversation_id)
    if held is None or held.question_message_id != question_id or repository.has_active_job(session, question_id):
        raise SkipNotAllowed
    failed = repository.lock_job(session, held.job_id)
    assert failed is not None
    failed.skipped_at = now
    session.flush()
    repository.delete_hold(session, conversation_id)
    changes = Changes()
    changes.add(failed.id, *repository.resume_questions(session, conversation_id))
    return changes


@dataclass(frozen=True)
class QueueCounts:
    """The `queue` part of the draft's `limits` object."""

    running: int
    questions_waiting: int
    retries_waiting: int
    questions_max: int = QUESTIONS_MAX
    retries_max: int = RETRIES_MAX


def queue_counts(session: Session, user_id: int) -> QueueCounts:
    """Read without the member lock; an admission may still be refused after it."""
    return QueueCounts(*repository.waiting_counts(session, user_id))


def members_with_work(session: Session, *, now: datetime | None = None) -> Sequence[int]:
    now = _now(session, now)
    return repository.members_with_work(session, now, now - timedelta(seconds=RECOVERY_GRACE_SECONDS))
