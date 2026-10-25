"""The signed-in member's chat jobs: list, read, cancel before they run, limits and the subscription (#53)."""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.auth import current_access, current_user_id
from app.api.chat_events import PING_SECONDS, ChatEvents, Closed, Opened
from app.api.chat_payloads import item_out, job_out, limits_out
from app.api.errors import ApiError, not_found
from app.api.schemas import Canceled, DecimalId, EmptyBody, ErrorBody, JobItem, JobPage, LimitsOut
from app.db import get_session, get_session_factory
from app.repositories import chat_jobs as job_repository
from app.repositories import conversations as conversation_repository
from app.security.tokens import VerifiedToken
from app.services import chat_queue, chat_views, quota

router = APIRouter(
    prefix="/api/chat",
    responses={401: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)

UserId = Annotated[int, Depends(current_user_id)]
DbSession = Annotated[Session, Depends(get_session)]
SessionFactory = Annotated[Callable[[], AbstractContextManager[Session]], Depends(get_session_factory)]
Limit = Annotated[int, Query(ge=1, le=100)]

# The quota as the queue's ledger; it keeps no state of its own.
LEDGER = quota.MemberQuota()


def chat_events(request: Request) -> ChatEvents:
    """The app's subscriptions; an app without them gets a fresh hub that nobody listens to."""
    events = getattr(request.app.state, "chat_events", None)
    if not isinstance(events, ChatEvents):
        events = ChatEvents()
        request.app.state.chat_events = events
    return events


def after_commit(request: Request, session: Session, user_id: int, job_ids: Sequence[int]) -> None:
    """Announce committed job changes to the member's subscriptions and wake the worker."""
    events = chat_events(request)
    events.publish(session, user_id, job_ids)
    events.wake()


def current_limits(session: Session, user_id: int) -> LimitsOut:
    return limits_out(chat_views.limits(session, user_id))


def quota_refusal(error: quota.QuotaRejected) -> ApiError:
    return ApiError(error.reason, retry_after=error.retry_after)


@router.get("/jobs", responses={404: {"model": ErrorBody}})
def list_jobs(
    user_id: UserId,
    session: DbSession,
    conversation_id: Annotated[DecimalId | None, Query()] = None,
    limit: Limit = 20,
    before_id: Annotated[DecimalId | None, Query()] = None,
) -> JobPage:
    """Without a conversation: every job that has not ended. With one: its jobs, newest first, in pages."""
    server_time = job_repository.database_time(session)
    next_cursor = None
    if conversation_id is None:
        jobs = list(job_repository.active_jobs(session, user_id))
    else:
        if not conversation_repository.owns_conversation(session, user_id, conversation_id):
            raise not_found()
        rows = job_repository.conversation_jobs(session, conversation_id, limit=limit + 1, before_id=before_id)
        jobs = list(rows[:limit])
        next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    items = [job_out(view) for view in chat_views.views(session, user_id, jobs)]
    return JobPage(
        items=items, next_cursor=next_cursor, limits=current_limits(session, user_id), server_time=server_time
    )


@router.get("/jobs/{job_id}", responses={404: {"model": ErrorBody}})
def read_job(job_id: Annotated[DecimalId, Path()], user_id: UserId, session: DbSession) -> JobItem:
    """The job with its question and, once it ended, its stored final answer."""
    job = job_repository.owned_job(session, user_id, job_id)
    if job is None:
        raise not_found()
    return item_out(chat_views.items(session, user_id, [job])[0])


@router.post("/jobs/{job_id}/cancel", responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}})
def cancel_job(
    job_id: Annotated[DecimalId, Path()], _body: EmptyBody, user_id: UserId, session: DbSession, request: Request
) -> Canceled:
    """Cancel a job that has not started and refund its reservation; a canceled job is returned again as it is."""
    try:
        job, changes = chat_queue.cancel(session, LEDGER, user_id, job_id)
    except chat_queue.JobNotFound:
        raise not_found() from None
    except chat_queue.JobNotCancelable:
        raise ApiError("job_not_cancelable") from None
    session.commit()
    (view,) = chat_views.views(session, user_id, [job])
    body = Canceled(job=job_out(view), limits=current_limits(session, user_id))
    after_commit(request, session, user_id, changes.job_ids)
    return body


@router.get("/limits")
def read_limits(user_id: UserId, session: DbSession) -> LimitsOut:
    return current_limits(session, user_id)


async def event_stream(
    events: ChatEvents,
    opened: Opened,
    open_session: Callable[[], AbstractContextManager[Session]],
    access_expires_at: datetime,
) -> AsyncIterator[str]:
    """The snapshot, then the member's events as they come, a ping every 15 seconds and `limits` after a reset.

    The stream ends when the access token it was opened with expires, so no event
    reaches a connection whose token is no longer valid; the screen reconnects with a
    valid one. Ending it never stops a job.
    """
    subscription = opened.subscription
    next_reset = opened.next_reset

    def refresh() -> datetime:
        with open_session() as session:
            try:
                return events.refresh_limits(session, subscription)
            finally:
                session.rollback()

    try:
        while True:
            left = (access_expires_at - datetime.now(UTC)).total_seconds()
            if left <= 0:
                return
            frame = None
            with anyio.move_on_after(min(PING_SECONDS, left)):
                frame = await subscription.get()
            if frame is None:
                if datetime.now(UTC) >= access_expires_at:
                    return
                if datetime.now(UTC) >= next_reset:
                    next_reset = await run_in_threadpool(refresh)
                yield ": ping\n\n"
            elif isinstance(frame, Closed):
                return
            else:
                yield frame
    finally:
        events.close(subscription)


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "`snapshot`, then `job`, `delta` and `limits` events.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def subscribe(
    request: Request,
    access: Annotated[VerifiedToken, Depends(current_access)],
    open_session: SessionFactory,
    since: Annotated[AwareDatetime | None, Query()] = None,
) -> StreamingResponse:
    """Open the member's subscription until its access token expires; closing it never stops a job.

    `since` is the `server_time` of the last job list or snapshot. The snapshot then
    also carries the jobs that ended, or whose `allowed_actions` changed, after it.
    """
    user_id = access.user_id
    events = chat_events(request)
    loop = asyncio.get_running_loop()

    def start() -> Opened:
        with open_session() as session:
            try:
                return events.open(session, user_id, loop, since)
            finally:
                session.rollback()

    opened = await run_in_threadpool(start)
    return StreamingResponse(
        event_stream(events, opened, open_session, access.expires_at),
        media_type="text/event-stream",
        # A proxy must pass each event on as it is written.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        # Also closes a subscription whose body never started.
        background=BackgroundTask(events.close, opened.subscription),
    )
