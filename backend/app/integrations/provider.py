"""The answer provider that the chat service calls, and its free mock.

The mock makes no network call and costs nothing. The real adapter is a later
issue; it has to keep this interface and its two failures.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class ProviderFailed(Exception):
    """The provider answered that it cannot produce an answer."""


class ProviderTimeout(Exception):
    """No answer arrived within the time limit, so the outcome is unknown."""


@dataclass(frozen=True)
class ContextMessage:
    role: str
    content: str


class AnswerProvider(Protocol):
    def complete(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> str:
        """Return the whole answer to the last message, or raise one of the two failures."""
        ...


class MockProvider:
    """Always the same completed answer, immediately."""

    def complete(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> str:
        return "모의 답변입니다."


def get_provider() -> AnswerProvider:
    return MockProvider()
