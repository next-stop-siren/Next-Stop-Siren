"""Owner-limited conversation storage that the conversation API calls.

The signatures are the contract of the API. The bodies need the conversation and
message ORM models of #9 and the ownership queries of #8, so they are not implemented yet.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session


class ConversationRow(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def updated_at(self) -> datetime: ...


class MessageRow(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def role(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def content(self) -> str | None: ...
    @property
    def request_key(self) -> str | None: ...
    @property
    def reply_to_message_id(self) -> int | None: ...
    @property
    def attempt_no(self) -> int | None: ...
    @property
    def error_code(self) -> str | None: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def updated_at(self) -> datetime: ...
    @property
    def completed_at(self) -> datetime | None: ...


def create_conversation(session: Session, user_id: int) -> ConversationRow:
    """Add a conversation owned by the user and load its generated ID and times.

    The caller commits.
    """
    raise NotImplementedError("Requires the conversation ORM model of #9")


def list_conversations(
    session: Session, user_id: int, *, limit: int, before_id: int | None
) -> Sequence[ConversationRow]:
    """Return at most `limit` of the user's conversations, newest ID first, below `before_id`."""
    raise NotImplementedError("Requires the ownership query functions of #8")


def list_messages(
    session: Session, user_id: int, conversation_id: int, *, limit: int, after_id: int | None
) -> Sequence[MessageRow] | None:
    """Return at most `limit` messages in ascending ID order, above `after_id`.

    None means the conversation does not exist or belongs to another user; the two
    cases must not be distinguishable.
    """
    raise NotImplementedError("Requires the ownership query functions of #8")
