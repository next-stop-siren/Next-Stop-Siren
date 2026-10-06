"""Request and response models of the conversation API, separate from ORM models."""

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

MAX_ID = 2**63 - 1


def parse_decimal_id(value: object) -> int:
    """Accept only the decimal string form of a positive bigint."""
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 19:
        raise ValueError("ID must be a decimal string")
    number = int(value)
    if value.startswith("0") or number > MAX_ID:
        raise ValueError("ID is out of range")
    return number


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# A path or query ID received as a decimal string.
DecimalId = Annotated[int, BeforeValidator(parse_decimal_id)]
# A bigint sent as a decimal string, because JavaScript numbers cannot hold every bigint.
IdText = Annotated[str, BeforeValidator(lambda value: str(value) if isinstance(value, int) else value)]
UtcTime = Annotated[datetime, PlainSerializer(utc_text, return_type=str)]


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdText
    created_at: UtcTime
    updated_at: UtcTime


class ConversationCreated(BaseModel):
    conversation: ConversationOut


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None


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
