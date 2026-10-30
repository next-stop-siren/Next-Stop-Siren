"""The real answer provider: Codyssey's GPT API with the fixed gpt-5.5 model (S11, #68).

Nothing in the public service selects this adapter yet; `get_provider()` stays the
free mock until switching to the real AI is approved separately. It is used by the
stand-in server checks and the explicit, isolated smoke test.

One call is one HTTP request. There is no automatic retry and no fallback to another
model or provider. The caller's wait ends at the deadline, when the connection is
closed; a connection still opening then is closed without sending the request.
Whether the provider stops generating or charging is not guaranteed. Only an answer
that ended naturally (`finish_reason` "stop") is returned as complete; `stream`
raises `ProviderFailed` after its pieces when the end was not natural, so the queue
never stores a partial answer as completed.

Logs carry the HTTP status, an end reason, a piece count and elapsed time only,
never the key, a question, an answer or the provider's response body.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException, HTTPResponse, HTTPSConnection
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
# Unfinished reasons logged by name; any other value is logged as "other", never echoed.
KNOWN_UNFINISHED = frozenset({"length", "content_filter", "tool_calls", "function_call"})


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


class _EndOfBody:
    """The response body ended; for a stream, before or after its `[DONE]` line."""


class _Call:
    """One HTTP request on a helper thread, read through a queue until a deadline.

    The caller's wait never blocks on the socket, so the deadline holds however slowly
    the server answers. `cancel` closes the socket under the request; what the helper
    thread puts on the queue after that is never read.
    """

    def __init__(
        self, settings: GptSettings, messages: Sequence[ContextMessage], timeout_seconds: float, *, stream: bool
    ):
        url = urlsplit(settings.base_url.rstrip("/") + "/chat/completions")
        connection_type = HTTPSConnection if url.scheme == "https" else HTTPConnection
        self.connection = connection_type(url.netloc, timeout=timeout_seconds)
        # A closed connection must not reopen by itself and send the request after the deadline.
        self.connection.auto_open = 0
        payload: dict[str, object] = {
            "model": settings.model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
        }
        if stream:
            payload["stream"] = True
        self.path = url.path
        self.body = json.dumps(payload, ensure_ascii=False).encode()
        self.headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        self.events: queue.Queue[object] = queue.Queue()
        # Held only to decide whether to send and to mark the call cancelled, never during I/O,
        # so cancelling does not wait for a slow send. A connection that opens after the
        # deadline sees the mark and sends nothing; a send under way when it passes fails on
        # the shut-down socket, which `auto_open = 0` keeps from reopening.
        self.guard = threading.Lock()
        self.cancelled = False
        # The opened socket, kept here because `getresponse` drops the connection's own
        # reference when the body ends at the close of the connection, while the response
        # still reads from it.
        self.sock: socket.socket | None = None
        self.started = time.monotonic()
        self.deadline = self.started + timeout_seconds

    def start(self, read: Callable[[HTTPResponse, Callable[[object], None]], None]) -> None:
        def request() -> None:
            try:
                self.connection.connect()
                with self.guard:
                    send = not self.cancelled
                    self.sock = self.connection.sock
                if not send:
                    self.connection.close()
                    return
                self.connection.request("POST", self.path, body=self.body, headers=self.headers)
                read(self.connection.getresponse(), self.events.put)
            except BaseException as error:
                self.events.put(error)

        threading.Thread(target=request, daemon=True).start()

    def next(self) -> object:
        """The next event before the deadline; after it, cancel and raise `ProviderTimeout`.

        An event already waiting on the queue is not read once the deadline has passed,
        so a reader that falls behind cannot finish a stream from the buffer late.
        """
        remaining = self.deadline - time.monotonic()
        if remaining > 0:
            try:
                item = self.events.get(timeout=remaining)
            except queue.Empty:
                pass
            else:
                if time.monotonic() <= self.deadline:
                    return item
        self.cancel()
        raise ProviderTimeout from None

    def cancel(self) -> None:
        """Mark the call cancelled and shut the socket down, so a waiting read or send ends now."""
        with self.guard:
            self.cancelled = True
            sock = self.sock
        _shutdown(sock)
        _cancel(self.connection)

    def elapsed_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)


def _read_whole(response: HTTPResponse, put: Callable[[object], None]) -> None:
    put(_Response(response.status, response.read()))


def _read_lines(response: HTTPResponse, put: Callable[[object], None]) -> None:
    """The status first, then each line of a 200 body as it arrives, then the end of the body."""
    put(_Response(response.status, b""))
    if response.status == 200:
        for line in response:
            put(line)
    else:
        response.read()
    put(_EndOfBody())


def _unfinished(finish_reason: object) -> str:
    # An array or object cannot be looked up in the set, so only a string is named.
    if isinstance(finish_reason, str) and finish_reason in KNOWN_UNFINISHED:
        return finish_reason
    return "other"


def _transport_failure(error: BaseException, call: _Call, mode: str) -> BaseException:
    """The provider failure that a transport error stands for; anything else is returned as it is."""
    if isinstance(error, (TimeoutError, socket.timeout)):
        logger.warning("event=gpt_call_ended mode=%s outcome=timeout elapsed_ms=%s", mode, call.elapsed_ms())
        return ProviderTimeout()
    if isinstance(error, (OSError, HTTPException)):
        logger.warning(
            "event=gpt_call_ended mode=%s outcome=connection_error error_type=%s elapsed_ms=%s",
            mode,
            type(error).__name__,
            call.elapsed_ms(),
        )
        return ProviderFailed()
    return error


class CodysseyGptProvider:
    def __init__(self, settings: GptSettings) -> None:
        self.settings = settings

    def complete(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> str:
        """Return the answer, or raise `ProviderFailed` or `ProviderTimeout` within `timeout_seconds`."""
        call = _Call(self.settings, messages, timeout_seconds, stream=False)
        call.start(_read_whole)
        try:
            result = call.next()
        except ProviderTimeout:
            logger.warning("event=gpt_call_ended mode=complete outcome=timeout elapsed_ms=%s", call.elapsed_ms())
            raise
        call.connection.close()
        if isinstance(result, BaseException):
            raise _transport_failure(result, call, "complete") from None
        assert isinstance(result, _Response)
        elapsed_ms = call.elapsed_ms()
        # 429 and quota exhaustion are failures like any other non-200 status; nothing is retried.
        if result.status != 200:
            logger.warning(
                "event=gpt_call_ended mode=complete outcome=http_error status=%s elapsed_ms=%s",
                result.status,
                elapsed_ms,
            )
            raise ProviderFailed
        try:
            choice = json.loads(result.body)["choices"][0]
            content, finish_reason = choice["message"]["content"], choice.get("finish_reason")
        except (ValueError, LookupError, TypeError, AttributeError):
            content, finish_reason = None, None
        if not isinstance(content, str):
            logger.warning("event=gpt_call_ended mode=complete outcome=invalid_response elapsed_ms=%s", elapsed_ms)
            raise ProviderFailed
        # Only a natural end is a whole answer; one cut by the length limit or a content
        # filter, with no stated reason or with a reason that is not a string, is stored as
        # a failure, never as completed.
        if finish_reason != "stop":
            logger.warning(
                "event=gpt_call_ended mode=complete outcome=incomplete finish_reason=%s elapsed_ms=%s",
                _unfinished(finish_reason),
                elapsed_ms,
            )
            raise ProviderFailed
        logger.info("event=gpt_call_ended mode=complete outcome=completed status=200 elapsed_ms=%s", elapsed_ms)
        return content

    def stream(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> Iterator[str]:
        """Yield the answer's pieces as the server sends them; raise a failure for any other end.

        The server sends `data:` lines (server-sent events); each holds a chunk with
        `choices[0].delta.content`, the last chunk names the `finish_reason`, and a
        `data: [DONE]` line ends the stream. The stream is complete only when `[DONE]`
        follows a "stop" reason. A body that ends without `[DONE]`, an error chunk, a
        chunk that is not JSON, or any other reason is `ProviderFailed`, even after
        pieces were yielded. `timeout_seconds` limits the whole stream, and closing
        the generator early cancels the request the same way.
        """
        call = _Call(self.settings, messages, timeout_seconds, stream=True)
        call.start(_read_lines)
        pieces = 0
        finish_reason: object = None
        try:
            status = self._next(call)
            assert isinstance(status, _Response)
            if status.status != 200:
                logger.warning(
                    "event=gpt_call_ended mode=stream outcome=http_error status=%s elapsed_ms=%s",
                    status.status,
                    call.elapsed_ms(),
                )
                raise ProviderFailed
            while True:
                line = self._next(call)
                if isinstance(line, _EndOfBody):
                    self._log_incomplete(call, "no_done", pieces)
                    raise ProviderFailed
                assert isinstance(line, bytes)
                data = _event_data(line)
                if data is None:
                    continue
                if data == b"[DONE]":
                    if finish_reason != "stop":
                        self._log_incomplete(call, _unfinished(finish_reason), pieces)
                        raise ProviderFailed
                    logger.info(
                        "event=gpt_call_ended mode=stream outcome=completed status=200 pieces=%s elapsed_ms=%s",
                        pieces,
                        call.elapsed_ms(),
                    )
                    return
                content, reason = self._chunk(call, data, pieces)
                if reason is not None:
                    finish_reason = reason
                if content:
                    pieces += 1
                    yield content
        finally:
            # After `[DONE]`, a failure, the deadline or a caller that stopped reading, the
            # connection ends here, so the helper thread never keeps reading.
            call.cancel()

    def _next(self, call: _Call) -> object:
        try:
            item = call.next()
        except ProviderTimeout:
            logger.warning("event=gpt_call_ended mode=stream outcome=timeout elapsed_ms=%s", call.elapsed_ms())
            raise
        if isinstance(item, BaseException):
            raise _transport_failure(item, call, "stream") from None
        return item

    def _chunk(self, call: _Call, data: bytes, pieces: int) -> tuple[str | None, object]:
        """The piece and the finish reason of one chunk; an error or malformed chunk is a failure."""
        try:
            chunk = json.loads(data)
        except ValueError:
            chunk = None
        if not isinstance(chunk, dict) or "error" in chunk:
            outcome = "provider_error" if isinstance(chunk, dict) else "invalid_response"
            logger.warning(
                "event=gpt_call_ended mode=stream outcome=%s pieces=%s elapsed_ms=%s",
                outcome,
                pieces,
                call.elapsed_ms(),
            )
            raise ProviderFailed
        choices = chunk.get("choices")
        # A chunk without choices, such as a usage report, carries no piece.
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None, None
        delta = choices[0].get("delta")
        content = delta.get("content") if isinstance(delta, dict) else None
        return (content if isinstance(content, str) else None), choices[0].get("finish_reason")

    def _log_incomplete(self, call: _Call, reason: str, pieces: int) -> None:
        logger.warning(
            "event=gpt_call_ended mode=stream outcome=incomplete finish_reason=%s pieces=%s elapsed_ms=%s",
            reason,
            pieces,
            call.elapsed_ms(),
        )


def _event_data(line: bytes) -> bytes | None:
    """The value of a `data:` line; None for blank lines, comments and other fields."""
    line = line.rstrip(b"\r\n")
    if not line.startswith(b"data:"):
        return None
    return line[len(b"data:") :].strip()


def _shutdown(sock: socket.socket | None) -> None:
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _cancel(connection: HTTPConnection) -> None:
    """Close the socket under the waiting request, so that it ends and its late result is discarded."""
    _shutdown(connection.sock)
    connection.close()
