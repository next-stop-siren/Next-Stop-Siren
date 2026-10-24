"""Create, list and read the signed-in user's own conversations, and queue questions, retries and skips.

A question or retry is admitted to the member's chat queue together with its quota
reservation and answered `202` without waiting for the answer (#53). The answer
arrives through `GET /api/chat/events` and is read again from `GET /api/chat/jobs/{id}`.
"""

import logging
from collections.abc import Sequence
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.auth import current_user_id
from app.api.chat import LEDGER, after_commit, current_limits, quota_refusal
from app.api.chat_payloads import job_out, message_out
from app.api.errors import ApiError, ApiErrorCode, not_found
from app.api.schemas import (
    Accepted,
    ConversationCreated,
    ConversationOut,
    ConversationPage,
    DecimalId,
    EmptyBody,
    ErrorBody,
    MessageOut,
    MessagePage,
    QuestionIn,
    RetryIn,
    Skipped,
)
from app.db import get_session
from app.models.chat_job import ACTIVE_STATES
from app.observability import request_trace_id
from app.repositories import chat_jobs as job_repository
from app.repositories import conversations as repository
from app.services import chat, chat_queue, chat_views, quota

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/conversations",
    responses={401: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)

# The user ID comes first so that an unauthenticated request never opens a session.
UserId = Annotated[int, Depends(current_user_id)]
DbSession = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=100)]

# The HTTP error code of each refusal of the chat queue.
QUEUE_ERRORS: dict[type[Exception], ApiErrorCode] = {
    chat.RequestKeyConflict: "request_key_conflict",
    chat.RetryNotAllowed: "retry_not_allowed",
    chat.NotLatestQuestion: "retry_not_latest",
    chat_queue.QueueFull: "queue_full",
    chat_queue.RetryQueueFull: "retry_queue_full",
    chat_queue.SkipNotAllowed: "skip_not_allowed",
}


def refusal(error: Exception) -> ApiError:
    """A missing or another member's conversation or question is the same 404."""
    if isinstance(error, (chat.ConversationNotFound, chat.QuestionNotFound)):
        return not_found()
    if isinstance(error, quota.QuotaRejected):
        return quota_refusal(error)
    return ApiError(QUEUE_ERRORS[type(error)])


# Every refusal the queue and the quota raise before anything new is stored.
REFUSALS = (chat.ChatError, chat_queue.QueueError, quota.QuotaRejected)


class HasId(Protocol):
    @property
    def id(self) -> int: ...


def next_cursor(rows: Sequence[HasId], limit: int) -> str | None:
    """The last returned ID when one more row than the page size was found."""
    return str(rows[limit - 1].id) if len(rows) > limit else None


@router.post("", status_code=201)
def create_conversation(_body: EmptyBody, user_id: UserId, session: DbSession) -> ConversationCreated:
    row = repository.create_conversation(session, user_id)
    session.commit()
    return ConversationCreated(conversation=ConversationOut.model_validate(row))


@router.get("")
def list_conversations(
    user_id: UserId,
    session: DbSession,
    limit: Limit = 20,
    before_id: Annotated[DecimalId | None, Query()] = None,
) -> ConversationPage:
    rows = repository.list_conversations(session, user_id, limit=limit + 1, before_id=before_id)
    items = [ConversationOut.model_validate(row) for row in rows[:limit]]
    return ConversationPage(items=items, next_cursor=next_cursor(rows, limit))


@router.get("/{conversation_id}/messages", responses={404: {"model": ErrorBody}})
def list_messages(
    conversation_id: Annotated[DecimalId, Path()],
    user_id: UserId,
    session: DbSession,
    limit: Limit = 20,
    after_id: Annotated[DecimalId | None, Query()] = None,
) -> MessagePage:
    rows = repository.list_messages(session, user_id, conversation_id, limit=limit + 1, after_id=after_id)
    if rows is None:
        raise not_found()
    items = [MessageOut.model_validate(row) for row in rows[:limit]]
    return MessagePage(items=items, next_cursor=next_cursor(rows, limit))


def accepted(session: Session, user_id: int, admission: chat_queue.Admission, response: Response) -> Accepted:
    """`202` while the job has not ended, `200` for a stored one that has; read after the commit."""
    job = admission.job
    if job is None:
        # A question or retry the synchronous API stored before the queue: it has its answer but no job.
        answer = admission.answer or job_repository.latest_answer(session, admission.question.id)
        response.status_code = 202 if answer is None or answer.status == "pending" else 200
        return Accepted(
            question=MessageOut.model_validate(admission.question),
            answer=message_out(answer),
            job=None,
            limits=current_limits(session, user_id),
        )
    (found,) = chat_views.items(session, user_id, [job])
    response.status_code = 202 if admission.created or job.state in ACTIVE_STATES else 200
    return Accepted(
        question=MessageOut.model_validate(found.question),
        answer=message_out(found.answer),
        job=job_out(found.view),
        limits=current_limits(session, user_id),
    )


ADMISSION_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {"model": Accepted, "description": "The key was already admitted and its job has ended."},
    202: {"model": Accepted, "description": "Admitted, or the key was already admitted and its job has not ended."},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    429: {"model": ErrorBody, "description": "`quota_exceeded` or `attempt_too_soon`, with `Retry-After`."},
}


@router.post("/{conversation_id}/messages", status_code=202, responses=ADMISSION_RESPONSES)
def send_question(
    conversation_id: Annotated[DecimalId, Path()],
    body: QuestionIn,
    user_id: UserId,
    session: DbSession,
    request: Request,
    response: Response,
) -> Accepted:
    """Queue a question with its quota reservation; the same key and body return the stored job instead."""
    trace_id = request_trace_id(request)
    try:
        admission = chat_queue.admit_question(session, LEDGER, user_id, conversation_id, body.content, body.request_key)
    except REFUSALS as error:
        raise refusal(error) from None
    session.commit()
    if admission.created and admission.job is not None:
        logger.info("Question queued, trace_id=%s, job_id=%s", trace_id, admission.job.id)
    result = accepted(session, user_id, admission, response)
    if admission.created and admission.job is not None:
        after_commit(request, session, user_id, [admission.job.id])
    return result


@router.post("/{conversation_id}/messages/{message_id}/retry", status_code=202, responses=ADMISSION_RESPONSES)
def retry_answer(
    conversation_id: Annotated[DecimalId, Path()],
    message_id: Annotated[DecimalId, Path()],
    body: RetryIn,
    user_id: UserId,
    session: DbSession,
    request: Request,
    response: Response,
) -> Accepted:
    """Queue a new attempt of the question `message_id`; the same retry key returns that attempt instead."""
    trace_id = request_trace_id(request)
    try:
        admission = chat_queue.admit_retry(session, LEDGER, user_id, conversation_id, message_id, body.retry_key)
    except REFUSALS as error:
        raise refusal(error) from None
    session.commit()
    if admission.created and admission.job is not None:
        logger.info("Retry queued, trace_id=%s, job_id=%s", trace_id, admission.job.id)
    result = accepted(session, user_id, admission, response)
    if admission.created and admission.job is not None:
        after_commit(request, session, user_id, [admission.job.id])
    return result


@router.post(
    "/{conversation_id}/messages/{message_id}/skip",
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def skip_question(
    conversation_id: Annotated[DecimalId, Path()],
    message_id: Annotated[DecimalId, Path()],
    _body: EmptyBody,
    user_id: UserId,
    session: DbSession,
    request: Request,
) -> Skipped:
    """Leave the failed question as it is and queue the questions paused behind it; uses no quota."""
    try:
        changes = chat_queue.skip(session, user_id, conversation_id, message_id)
    except REFUSALS as error:
        raise refusal(error) from None
    session.commit()
    changed = job_repository.owned_jobs(session, user_id, changes.job_ids)
    resumed = [job for job in changed if job.question_message_id != message_id]
    body = Skipped(
        skipped_message_id=str(message_id),
        resumed_jobs=[job_out(view) for view in chat_views.views(session, user_id, resumed)],
        limits=current_limits(session, user_id),
    )
    after_commit(request, session, user_id, changes.job_ids)
    return body
