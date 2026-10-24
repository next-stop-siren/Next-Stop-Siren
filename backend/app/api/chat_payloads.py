"""Turn the chat views of app.services.chat_views into the public response models."""

from typing import cast

from app.api.schemas import JobAction, JobItem, JobOut, JobState, LimitsOut, MessageOut, QueueOut, WindowOut
from app.models.conversation import Message
from app.services import chat_views
from app.services.quota import WindowLimit


def job_out(view: chat_views.JobView) -> JobOut:
    job = view.job
    return JobOut(
        id=str(job.id),
        kind="retry" if job.kind == "retry" else "question",
        conversation_id=str(job.conversation_id),
        question_message_id=str(job.question_message_id),
        answer_message_id=None if job.answer_message_id is None else str(job.answer_message_id),
        state=cast(JobState, job.state),
        position=view.position,
        paused_by_message_id=None if job.paused_by_message_id is None else str(job.paused_by_message_id),
        error_code=view.error_code,
        accepted_at=job.accepted_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        expires_at=job.expires_at,
        allowed_actions=[cast(JobAction, action) for action in view.allowed_actions],
    )


def message_out(message: Message | None) -> MessageOut | None:
    return None if message is None else MessageOut.model_validate(message)


def item_out(found: chat_views.JobItem) -> JobItem:
    return JobItem(
        job=job_out(found.view), question=MessageOut.model_validate(found.question), answer=message_out(found.answer)
    )


def _window(window: WindowLimit) -> WindowOut:
    return WindowOut(
        limit=window.limit,
        used=window.used,
        reserved=window.reserved,
        remaining=window.remaining,
        resets_at=window.resets_at,
    )


def limits_out(limits: chat_views.Limits) -> LimitsOut:
    queue = limits.queue
    return LimitsOut(
        daily=_window(limits.quota.daily),
        six_hour=_window(limits.quota.six_hour),
        queue=QueueOut(
            running=queue.running,
            questions_waiting=queue.questions_waiting,
            questions_max=queue.questions_max,
            retries_waiting=queue.retries_waiting,
            retries_max=queue.retries_max,
        ),
        next_attempt_at=limits.quota.next_attempt_at,
    )
