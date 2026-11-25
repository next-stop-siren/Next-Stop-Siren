"""The answer provider that the chat service calls, and its free mock.

The mock makes no network call and costs nothing. Only an explicit server-side
`CHAT_PROVIDER=codyssey` selects the real adapter; invalid settings never fall back
to mock. Constructing either provider makes no network call.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
import os
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


class ProviderSettingsError(ValueError):
    """Invalid provider selection, without echoing environment values."""


def get_provider(environ: Mapping[str, str] | None = None) -> AnswerProvider:
    """Select once at worker startup; omission means mock, every explicit value is checked."""
    values = os.environ if environ is None else environ
    selected = values.get("CHAT_PROVIDER", "mock")
    if selected == "mock":
        return MockProvider()
    if selected == "codyssey":
        # The adapter imports the protocol types above; load it only after this module is ready.
        from app.integrations.codyssey import CodysseyGptProvider, GptSettings

        return CodysseyGptProvider(GptSettings.from_env(values))
    raise ProviderSettingsError("CHAT_PROVIDER: mock 또는 codyssey를 정확히 지정해 주세요.")
