"""Create, list and read the signed-in user's own conversations, and send questions to them."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractContextManager
from typing import Annotated, Protocol

import anyio
from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import current_user_id
from app.api.errors import (
    DATABASE_UNAVAILABLE,
    ApiError,
    api_error_body,
    conversation_busy,
    database_unavailable_body,
    not_found,
    provider_failed,
    provider_timeout,
    request_key_conflict,
)
from app.api.schemas import (
    AnswerDelta,
    ConversationCreated,
    ConversationOut,
    ConversationPage,
    DecimalId,
    EmptyBody,
    ErrorBody,
    MessageOut,
    MessagePage,
    QuestionAnswer,
    QuestionIn,
)
from app.db import get_session, get_session_factory
from app.integrations.provider import AnswerProvider, get_provider
from app.observability import request_trace_id
from app.repositories import conversations as repository
from app.services import chat

router = APIRouter(
    prefix="/api/conversations",
    responses={401: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)

# The user ID comes first so that an unauthenticated request never opens a session.
UserId = Annotated[int, Depends(current_user_id)]
DbSession = Annotated[Session, Depends(get_session)]
# Opens the session of a streamed answer's last transaction, after the request's own has closed.
SessionFactory = Annotated[Callable[[], AbstractContextManager[Session]], Depends(get_session_factory)]
Provider = Annotated[AnswerProvider, Depends(get_provider)]

# The HTTP answer to each refusal of the chat service.
CHAT_ERRORS: dict[type[chat.ChatError], Callable[[], ApiError]] = {
    chat.ConversationNotFound: not_found,
    chat.RequestKeyConflict: request_key_conflict,
    chat.ConversationBusy: conversation_busy,
}
Limit = Annotated[int, Query(ge=1, le=100)]


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


@router.post(
    "/{conversation_id}/messages",
    status_code=201,
    responses={
        200: {"model": QuestionAnswer, "description": "The request key was already stored and its answer has ended."},
        202: {"model": QuestionAnswer, "description": "The request key was already stored and its answer is pending."},
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
        502: {"model": ErrorBody},
        504: {"model": ErrorBody},
    },
)
def send_question(
    conversation_id: Annotated[DecimalId, Path()],
    body: QuestionIn,
    user_id: UserId,
    session: DbSession,
    provider: Provider,
    request: Request,
    response: Response,
) -> QuestionAnswer:
    # The request's trace ID: the service logs under it, and the error handlers return it
    # in a 502, 504 or database 503, so the response leads to its log entries.
    trace_id = request_trace_id(request)
    try:
        exchange = chat.send_question(
            session, provider, user_id, conversation_id, body.content, body.request_key, trace_id=trace_id
        )
    except chat.ChatError as error:
        raise CHAT_ERRORS[type(error)]() from None
    status = exchange.answer.status
    if not exchange.created:
        response.status_code = 202 if status == "pending" else 200
    elif status == "failed":
        raise provider_failed()
    elif status == "interrupted":
        raise provider_timeout()
    return QuestionAnswer(
        question=MessageOut.model_validate(exchange.question), answer=MessageOut.model_validate(exchange.answer)
    )


def sse(event: str, data: BaseModel) -> str:
    """One server-sent event; the JSON is a single line because JSON escapes line breaks."""
    return f"event: {event}\ndata: {data.model_dump_json()}\n\n"


def abandon(events: chat.AnswerStream, trace_id: str) -> None:
    """End a stream nobody reads any more; the service stores an unfinished answer as interrupted.

    If that cannot be stored, the answer stays pending and the next send recovers it.
    """
    try:
        events.close()
    except DATABASE_UNAVAILABLE as error:
        database_unavailable_body(error, trace_id)


async def answer_events(
    opening: str, events: chat.AnswerStream, question: MessageOut, trace_id: str
) -> AsyncIterator[str]:
    """The response body: `start`, a `delta` per piece, then `done` or `error`.

    `done` carries the answer as stored, so it is sent only after the commit. The status
    code has been sent by then, so an ending that the JSON route answers with 502, 504
    or 503 is an `error` event with the same body. A reader that leaves at any point,
    `start` included, reaches the `finally`, which closes the stream.
    """
    try:
        yield opening
        while True:
            try:
                item = await run_in_threadpool(next, events, None)
            except DATABASE_UNAVAILABLE as error:
                yield sse("error", database_unavailable_body(error, trace_id))
                return
            if item is None:
                return
            if isinstance(item, str):
                yield sse("delta", AnswerDelta(text=item))
            elif item.status == "completed":
                yield sse("done", QuestionAnswer(question=question, answer=MessageOut.model_validate(item)))
            else:
                ended = provider_failed() if item.status == "failed" else provider_timeout()
                yield sse("error", api_error_body(ended, trace_id))
    finally:
        # A reader that leaves cancels this task; the shield lets the unfinished answer be closed anyway.
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(abandon, events, trace_id)


@router.post(
    "/{conversation_id}/messages/stream",
    response_model=None,
    responses={
        200: {
            "model": QuestionAnswer,
            "description": "A new question: `start`, `delta` and `done` or `error` events. "
            "A request key already stored whose answer has ended: the stored pair as JSON.",
            "content": {"text/event-stream": {}},
        },
        202: {"model": QuestionAnswer, "description": "The request key was already stored and its answer is pending."},
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
    },
)
def stream_question(
    conversation_id: Annotated[DecimalId, Path()],
    body: QuestionIn,
    user_id: UserId,
    session: DbSession,
    open_session: SessionFactory,
    provider: Provider,
    request: Request,
) -> Response:
    """Send a question as `send_question` does, with the answer streamed while it is made.

    Everything refused or already stored is answered as JSON before a stream starts, so
    a screen that lost its stream sends the same key and body again to read what was stored.
    """
    trace_id = request_trace_id(request)
    try:
        begun = chat.begin_question(
            session, user_id, conversation_id, body.content, body.request_key, trace_id=trace_id
        )
    except chat.ChatError as error:
        raise CHAT_ERRORS[type(error)]() from None
    question = MessageOut.model_validate(begun.question)
    pair = QuestionAnswer(question=question, answer=MessageOut.model_validate(begun.answer))
    if isinstance(begun, chat.Exchange):
        status_code = 202 if begun.answer.status == "pending" else 200
        return JSONResponse(status_code=status_code, content=pair.model_dump(mode="json"))
    # Reading the committed rows for `start` may have begun a transaction, as a session
    # that expires rows on commit reloads them. The request session is not used again,
    # so close it now: no transaction may stay open while the provider is called.
    session.close()
    events = chat.AnswerStream(open_session, provider, begun, trace_id=trace_id)
    return StreamingResponse(
        answer_events(sse("start", pair), events, question, trace_id),
        media_type="text/event-stream",
        # A proxy must pass each event on as it is written.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
