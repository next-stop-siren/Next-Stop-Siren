"""What the member sees of their chat jobs: allowed actions, queue positions, limits and snapshots (#53).

Everything here only reads; the caller decides when its transaction ends. `user_id`
is always the ID the server verified, so another member's job is never returned.

`allowed_actions` is computed from the stored rows each time, never stored, so it
cannot drift from them. A job offers:

- `cancel` while it waits (`queued` or `paused`);
- `retry` when it failed or was interrupted, its answer is the newest attempt of its
  question, no attempt of that question waits or runs, and no later question of the
  conversation has started;
- `skip` besides, while the conversation is held by exactly this job.

A change to these always comes with an admission, start, end or skip of a job in the
same conversation, which is how `related_ids` and `snapshot(since=...)` find the jobs
whose buttons changed.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.chat_job import WAITING_STATES, ChatJob
from app.models.conversation import Message
from app.repositories import chat_jobs as repository
from app.services import chat_queue, quota

# Changes committed while a snapshot was read can carry times slightly before its
# `server_time`; a reconnect looks back this far so it does not miss them.
SINCE_MARGIN = timedelta(seconds=30)


@dataclass(frozen=True)
class JobView:
    job: ChatJob
    position: int | None
    allowed_actions: tuple[str, ...]
    # The stored answer's error code while the job is failed or interrupted.
    error_code: str | None


@dataclass(frozen=True)
class JobItem:
    view: JobView
    question: Message
    answer: Message | None


@dataclass(frozen=True)
class Limits:
    quota: quota.QuotaLimits
    queue: chat_queue.QueueCounts


@dataclass(frozen=True)
class Snapshot:
    items: list[JobItem]
    limits: Limits
    server_time: datetime


def execution_order(queued: Sequence[ChatJob]) -> list[ChatJob]:
    """The order `claim_next` runs queued jobs in: by ID, a retry before its conversation's new questions."""
    remaining = sorted(queued, key=lambda job: job.id)
    order: list[ChatJob] = []
    while remaining:
        retried = {job.conversation_id for job in remaining if job.kind == "retry"}
        chosen = next(job for job in remaining if job.kind == "retry" or job.conversation_id not in retried)
        order.append(chosen)
        remaining.remove(chosen)
    return order


def positions(session: Session, user_id: int) -> dict[int, int]:
    return {job.id: index for index, job in enumerate(execution_order(repository.queued_jobs(session, user_id)), 1)}


def allowed_actions(session: Session, job: ChatJob) -> tuple[str, ...]:
    if job.state in WAITING_STATES:
        return ("cancel",)
    if job.state not in ("failed", "interrupted"):
        return ()
    latest = repository.latest_answer(session, job.question_message_id)
    if latest is None or latest.id != job.answer_message_id:
        return ()
    if repository.has_active_job(session, job.question_message_id):
        return ()
    if repository.later_question_started(session, job.conversation_id, job.question_message_id):
        return ()
    held = repository.hold(session, job.conversation_id)
    return ("retry", "skip") if held is not None and held.job_id == job.id else ("retry",)


def _answer(session: Session, job: ChatJob) -> Message | None:
    return None if job.answer_message_id is None else repository.get_message(session, job.answer_message_id)


def view(session: Session, job: ChatJob, order: dict[int, int], answer: Message | None = None) -> JobView:
    if answer is None:
        answer = _answer(session, job)
    error_code = answer.error_code if answer is not None and job.state in ("failed", "interrupted") else None
    position = order.get(job.id) if job.state == "queued" else None
    return JobView(job, position, allowed_actions(session, job), error_code)


def item(session: Session, job: ChatJob, order: dict[int, int]) -> JobItem:
    answer = _answer(session, job)
    question = repository.get_message(session, job.question_message_id)
    return JobItem(view(session, job, order, answer), question, answer)


def views(session: Session, user_id: int, jobs: Iterable[ChatJob]) -> list[JobView]:
    order = positions(session, user_id)
    return [view(session, job, order) for job in jobs]


def items(session: Session, user_id: int, jobs: Iterable[ChatJob]) -> list[JobItem]:
    order = positions(session, user_id)
    return [item(session, job, order) for job in jobs]


def related_ids(session: Session, user_id: int, job_ids: Sequence[int]) -> list[int]:
    """The changed jobs, the member's queued jobs, whose positions move, and failed jobs whose buttons may change."""
    jobs = repository.owned_jobs(session, user_id, job_ids)
    conversations = sorted({job.conversation_id for job in jobs})
    ids = [job.id for job in jobs]
    for job in [*repository.queued_jobs(session, user_id), *repository.failed_recent_jobs(session, conversations)]:
        if job.id not in ids:
            ids.append(job.id)
    return sorted(ids)


def limits(session: Session, user_id: int) -> Limits:
    return Limits(quota.limits(session, user_id), chat_queue.queue_counts(session, user_id))


def snapshot(session: Session, user_id: int, since: datetime | None) -> Snapshot:
    """The member's jobs that have not ended and, with `since`, those that ended or changed buttons after it.

    `server_time` is read first, so a change committed after the reads is not older than it.
    """
    server_time = repository.database_time(session)
    jobs = {job.id: job for job in repository.active_jobs(session, user_id)}
    if since is not None:
        since = since - SINCE_MARGIN
        for job in repository.ended_since(session, user_id, since):
            jobs.setdefault(job.id, job)
        touched = repository.touched_conversations(session, user_id, since)
        # Includes an earlier failed attempt whose buttons a retry completed elsewhere took away.
        for job in repository.failed_recent_jobs(session, touched):
            jobs.setdefault(job.id, job)
    found = items(session, user_id, [jobs[job_id] for job_id in sorted(jobs)])
    return Snapshot(found, limits(session, user_id), server_time)
