"""The answer provider that the chat service calls, and its free mock.

The mock makes no network call and costs nothing. The real adapter is a later
issue; it has to keep this interface and its two failures.
"""

from collections.abc import Iterator, Sequence
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
        """Return the whole answer to the last message, or raise one of the two failures.

        An implementation must itself return or raise `ProviderTimeout` within
        `timeout_seconds`; receiving the value does not limit the call.
        """
        ...

    def stream(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> Iterator[str]:
        """Yield the answer to the last message in pieces, in order; joined, they are the whole answer.

        Either failure can be raised before the first piece or between two pieces.
        `timeout_seconds` limits the whole stream, under the same rule as `complete`.
        """
        ...


class MockProvider:
    """Always the same completed answer, immediately."""

    PIECES = ("모의 ", "답변", "입니다.")

    def complete(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> str:
        return "".join(self.PIECES)

    def stream(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> Iterator[str]:
        yield from self.PIECES


def get_provider() -> AnswerProvider:
    return MockProvider()
