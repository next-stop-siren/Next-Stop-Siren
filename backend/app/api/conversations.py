"""Create, list and read the signed-in user's own conversations, and send questions to them."""

from collections.abc import Sequence
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.orm import Session

from app.api.auth import current_user_id
from app.api.errors import not_found, provider_failed, provider_timeout
from app.api.schemas import (
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
from app.db import get_session
from app.integrations.provider import AnswerProvider, get_provider
from app.repositories import conversations as repository
from app.services import chat

router = APIRouter(
    prefix="/api/conversations",
    responses={401: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)

# The user ID comes first so that an unauthenticated request never opens a session.
UserId = Annotated[int, Depends(current_user_id)]
DbSession = Annotated[Session, Depends(get_session)]
Provider = Annotated[AnswerProvider, Depends(get_provider)]
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
    response: Response,
) -> QuestionAnswer:
    exchange = chat.send_question(session, provider, user_id, conversation_id, body.content, body.request_key)
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
