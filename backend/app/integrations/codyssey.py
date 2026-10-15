"""The real answer provider: Codyssey's GPT API with the fixed gpt-5.5 model (S11).

Nothing in the public service selects this adapter yet; `get_provider()` stays the
free mock until member limits (#15), integration (#19) and deployment are verified.
It is used only by the explicit, isolated smoke test.

One call is one HTTP request. There is no automatic retry and no fallback to another
model or provider. The caller's wait ends at the deadline, when the connection is
closed; whether the provider stops generating or charging is not guaranteed.

Logs carry the HTTP status and elapsed time only, never the key, a question, an
answer or the provider's response body.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import logging
import os
import queue
import socket
import threading
import time
from urllib.parse import urlsplit

from app.integrations.provider import ContextMessage, ProviderFailed, ProviderTimeout

logger = logging.getLogger(__name__)

# The approved values in docs/04-reference/ai.md.
APPROVED_BASE_URL = "https://copa.codyssey.kr/v1"
APPROVED_MODEL = "gpt-5.5"


class GptSettingsError(ValueError):
    """A missing or unapproved setting, named without echoing its value."""


@dataclass(frozen=True, slots=True)
class GptSettings:
    api_key: str = field(repr=False)
    base_url: str
    model: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "GptSettings":
        """Read the developer's own server-side key; the URL and model must be the approved ones."""
        values = os.environ if environ is None else environ
        api_key = values.get("CODYSSEY_API_KEY", "")
        if not api_key or any(character.isspace() or ord(character) < 32 for character in api_key):
            raise GptSettingsError("CODYSSEY_API_KEY: 서버 .env에 본인 키가 없거나 형식이 올바르지 않습니다.")
        if values.get("CODYSSEY_BASE_URL") != APPROVED_BASE_URL:
            raise GptSettingsError(f"CODYSSEY_BASE_URL: 승인된 {APPROVED_BASE_URL}만 사용할 수 있습니다.")
        if values.get("CODYSSEY_MODEL") != APPROVED_MODEL:
            raise GptSettingsError(f"CODYSSEY_MODEL: 승인된 {APPROVED_MODEL}만 사용할 수 있습니다.")
        return cls(api_key, APPROVED_BASE_URL, APPROVED_MODEL)


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    body: bytes


class CodysseyGptProvider:
    def __init__(self, settings: GptSettings) -> None:
        self.settings = settings

    def complete(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> str:
        """Return the answer, or raise `ProviderFailed` or `ProviderTimeout` within `timeout_seconds`."""
        url = urlsplit(self.settings.base_url.rstrip("/") + "/chat/completions")
        connection_type = HTTPSConnection if url.scheme == "https" else HTTPConnection
        connection = connection_type(url.netloc, timeout=timeout_seconds)
        body = json.dumps(
            {
                "model": self.settings.model,
                "messages": [{"role": message.role, "content": message.content} for message in messages],
            },
            ensure_ascii=False,
        ).encode()
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        outcome: queue.Queue[_Response | BaseException] = queue.Queue(maxsize=1)

        def request() -> None:
            try:
                connection.request("POST", url.path, body=body, headers=headers)
                response = connection.getresponse()
                outcome.put(_Response(response.status, response.read()))
            except BaseException as error:
                outcome.put(error)

        started = time.monotonic()
        threading.Thread(target=request, daemon=True).start()
        try:
            result = outcome.get(timeout=timeout_seconds)
        except queue.Empty:
            _cancel(connection)
            logger.warning("event=gpt_call_ended outcome=timeout elapsed_ms=%s", _elapsed_ms(started))
            raise ProviderTimeout from None
        connection.close()
        elapsed_ms = _elapsed_ms(started)
        if isinstance(result, (TimeoutError, socket.timeout)):
            logger.warning("event=gpt_call_ended outcome=timeout elapsed_ms=%s", elapsed_ms)
            raise ProviderTimeout from None
        if isinstance(result, (OSError, HTTPException)):
            logger.warning(
                "event=gpt_call_ended outcome=connection_error error_type=%s elapsed_ms=%s",
                type(result).__name__,
                elapsed_ms,
            )
            raise ProviderFailed from None
        if isinstance(result, BaseException):
            raise result
        # 429 and quota exhaustion are failures like any other non-200 status; nothing is retried.
        if result.status != 200:
            logger.warning("event=gpt_call_ended outcome=http_error status=%s elapsed_ms=%s", result.status, elapsed_ms)
            raise ProviderFailed
        try:
            content = json.loads(result.body)["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError):
            content = None
        if not isinstance(content, str):
            logger.warning("event=gpt_call_ended outcome=invalid_response elapsed_ms=%s", elapsed_ms)
            raise ProviderFailed
        logger.info("event=gpt_call_ended outcome=completed status=200 elapsed_ms=%s", elapsed_ms)
        return content


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _cancel(connection: HTTPConnection) -> None:
    """Close the socket under the waiting request, so that it ends and its late result is discarded."""
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    connection.close()
