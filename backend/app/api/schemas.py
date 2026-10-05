"""Request and response models of the conversation API, separate from ORM models."""
#api 입출력 형식 (Pydantic 모델) api- db 사이 번역?

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

MAX_ID = 2**63 - 1


def parse_decimal_id(value: object) -> int:
    # value =id 맞음 대신, object로 아무거나 다 받음! 아래 검사 통과하면 int로 변경
    # DecimalId 가 pydantic 변환 전에 가져가기 때문에! 일단 주고 
    """Accept only the decimal string form of a positive bigint."""
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 19:
        raise ValueError("ID must be a decimal string")
    number = int(value)
    if value.startswith("0") or number > MAX_ID:
        raise ValueError("ID is out of range")
    return number
#브라우저 안에서 안 깨지기 위해서 id는 문자열
#여기는 문자열 id를 받아서 int로 바꿔주는 역할!

def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")



DecimalId = Annotated[int, BeforeValidator(parse_decimal_id)]
#before.. (parse_decimal_id) : Pydantic으로 int 확인하기 전에 parse_decimal_id 통해서
#브라우저에게 받은 str을 int로 변환하는 것

# A bigint sent as a decimal string, because JavaScript numbers cannot hold every bigint.
IdText = Annotated[str, BeforeValidator(lambda value: str(value) if isinstance(value, int) else value)]
#브라우저에게 보내는 것! if vlaue=int 라면 value=str로 바꾸기

UtcTime = Annotated[datetime, PlainSerializer(utc_text, return_type=str)]
#DB 안에서 datetime 으로 있다가 브라우저로 나갈 때 str 로

class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
# {}만 받는다! BaseModel = json 형식으로 받고
# 마지막 forbid가 아예 key를 정의 안 했기에 모두 key 있으면 cut


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    #from_attributes : ORM 읽어오는 것

    id: IdText
    created_at: UtcTime
    updated_at: UtcTime


class ConversationCreated(BaseModel):
    conversation: ConversationOut


class ConversationPage(BaseModel):
    items: list[ConversationOut] #필드 이름
    next_cursor: str | None  # or(또는)


class MessageOut(BaseModel):
    """The public message; retry_key is not part of the first screen format."""

    model_config = ConfigDict(from_attributes=True)

    id: IdText
    role: Literal["user", "assistant"]
    status: Literal["pending", "completed", "failed", "interrupted"]
    content: str | None
    request_key: str | None
    reply_to_message_id: IdText | None
    attempt_no: int | None
    error_code: str | None
    created_at: UtcTime
    updated_at: UtcTime
    completed_at: UtcTime | None


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None
