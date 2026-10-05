"""Create, list and read the signed-in user's own conversations."""
#api endpoint에 따른 기능구현 
# post , get(conversation), get(message)

from collections.abc import Sequence
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.auth import current_user_id
from app.api.errors import not_found
from app.api.schemas import (
    ConversationCreated,
    ConversationOut,
    ConversationPage,
    DecimalId,
    EmptyBody,
    MessageOut,
    MessagePage,
)
from app.db import get_session
from app.repositories import conversations as repository

router = APIRouter(prefix="/api/conversations")

# The user ID comes first so that an unauthenticated request never opens a session.
UserId = Annotated[int, Depends(current_user_id)]
DbSession = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=100)]
#Annotated: 타입힌트, 값 구하는 방법
#Limit: 쿼리 문자열 1<= <=100 사이만!

class HasId(Protocol):
    @property
    def id(self) -> int: ...


def next_cursor(rows: Sequence[HasId], limit: int) -> str | None:
    """The last returned ID when one more row than the page size was found."""
    return str(rows[limit - 1].id) if len(rows) > limit else None


@router.post("", status_code=201) # 대화 만들기 insert -> commit
def create_conversation(_body: EmptyBody, user_id: UserId, session: DbSession) -> ConversationCreated:
    #Pydantic 모델에 따라 DB= json : EmptyBody로 검증
    #session: DbSession = session 모델임... session은 transaction 묶음? 관리자?
    #DbSession: session 모델로.. 요청 끝나면 sesion 닫아라
    row = repository.create_conversation(session, user_id)
    session.commit()
    return ConversationCreated(conversation=ConversationOut.model_validate(row))
    #DB 응답 모델로 변환 

@router.get("") # 대화 내용 가져오기
def list_conversations(
    user_id: UserId,
    session: DbSession,
    limit: Limit = 20,  # 한 페이지당 20개
    before_id: Annotated[DecimalId | None, Query()] = None,
    # id는 DecimalId(숫자) or None일 수도.. None이면 없다고 해라
#GET /api/conversations               → before_id = None   → 최신 20개
#GET /api/conversations?before_id=481 → before_id = "481"  → 481보다 오래된 20개
#GET /api/conversations?before_id=abc → 422 (DecimalId 검사 실패)
) -> ConversationPage:
    rows = repository.list_conversations(session, user_id, limit=limit + 1, before_id=before_id)
    # 뒤 페이지 내용이 있는지 확인하는 것! 
    # 최신 대화부터 가져옴! 1001~1030 있다면, 1030~1011을 가져옴
    items = [ConversationOut.model_validate(row) for row in rows[:limit]]
    return ConversationPage(items=items, next_cursor=next_cursor(rows, limit))
    #cursor 에서 limit-1을 가져오기에 limit=1이 정상화 됨

@router.get("/{conversation_id}/messages")  #메시지 가져오기
def list_messages(
    conversation_id: Annotated[DecimalId, Path()],
    user_id: UserId,
    session: DbSession,
    limit: Limit = 20,
    after_id: Annotated[DecimalId | None, Query()] = None,
) -> MessagePage:
    rows = repository.list_messages(session, user_id, conversation_id, limit=limit + 1, after_id=after_id)
    #before_id와 다르게 가장 오래된 대화부터 가져옴! 1001~1030 중 1001~1020까지
    if rows is None:
        raise not_found()
    items = [MessageOut.model_validate(row) for row in rows[:limit]]
    return MessagePage(items=items, next_cursor=next_cursor(rows, limit))
