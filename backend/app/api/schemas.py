"""Request and response models of the conversation API, separate from ORM models."""

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
)

MAX_ID = 2**63 - 1
# The approved S10 input limits in docs/04-reference/api.md, in Unicode code points.
MAX_QUESTION_LENGTH = 4000
MAX_REQUEST_KEY_LENGTH = 200
# What OpenAPI shows for an ID input; parse_decimal_id also enforces the bigint range.
ID_INPUT_SCHEMA = {
    "type": "string",
    "pattern": "^[1-9][0-9]{0,18}$",
    "description": "Decimal string of a positive bigint ID.",
    "examples": ["501"],
}


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
DecimalId = Annotated[int, BeforeValidator(parse_decimal_id), WithJsonSchema(ID_INPUT_SCHEMA)]
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


def storable_text(value: str) -> str:
    """PostgreSQL text cannot hold the NUL character."""
    if "\x00" in value:
        raise ValueError("text cannot contain NUL")
    return value


def not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("text cannot be only whitespace")
    return storable_text(value)


class QuestionIn(BaseModel):
    """A question as sent; it is stored and compared exactly as received."""

    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, Field(max_length=MAX_QUESTION_LENGTH), AfterValidator(not_blank)]
    request_key: Annotated[str, Field(min_length=1, max_length=MAX_REQUEST_KEY_LENGTH), AfterValidator(storable_text)]


class QuestionAnswer(BaseModel):
    question: MessageOut
    answer: MessageOut


class AnswerDelta(BaseModel):
    """One piece of an answer being streamed: temporary output, not a stored message."""

    text: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    fields: dict[str, str]
    trace_id: str


class ErrorBody(BaseModel):
    """The common error body that app.api.errors returns, described for OpenAPI."""

    error: ErrorDetail
