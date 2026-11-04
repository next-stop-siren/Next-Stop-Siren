"""The Codyssey adapter's `stream` and its connection to the queue runner, against a local stand-in (#68).

Nothing leaves this machine: the key, questions and answers are fake, and the
stand-in server sends server-sent events the way the chat completions API does.
Pieces, a natural end, an end that is not natural, failures, the deadline and 429 or
quota responses are reproduced without spending any quota. No real call is made.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from app.integrations.codyssey import APPROVED_MODEL, CodysseyGptProvider, GptSettings
from app.integrations.provider import ContextMessage, ProviderFailed, ProviderTimeout
from app.services import chat_queue, chat_runner
from app.services.chat_queue import Changes, Claimed, Outcome

KEY = "fixture-codyssey-key"
QUESTION = "합성 질문 fixture-question"
CONTEXT = [
    ContextMessage("user", "이전 질문"),
    ContextMessage("assistant", "이전 답변"),
    ContextMessage("user", QUESTION),
]
PRIVATE = (KEY, QUESTION, "이전 질문", "이전 답변", "합성 ", "답변 조각", "fixture-provider-detail")


def chunk(content=None, finish_reason=None):
    delta = {} if content is None else {"content": content}
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})


DONE = "data: [DONE]"
NATURAL = [chunk("합성 "), chunk("답변 조각"), chunk(finish_reason="stop"), DONE]


class FakeStreamingGpt:
    """Records each request and answers with `lines` as events, or `reply` for a non-200 status.

    With `hold_after`, the server stops after that many lines until `release` is set,
    then reports whether the client had closed the connection.
    """

    def __init__(self) -> None:
        self.requests: list[SimpleNamespace] = []
        self.lines: list[str] = NATURAL
        self.reply: tuple[int, bytes] | None = None
        self.hold_after: int | None = None
        self.release = threading.Event()
        self.handled = threading.Event()
        self.peer_closed: bool | None = None
        self.provider: CodysseyGptProvider


@pytest.fixture
def gpt():
    fake = FakeStreamingGpt()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            fake.requests.append(SimpleNamespace(path=self.path, headers=self.headers, body=json.loads(body)))
            if fake.reply is not None:
                status, payload = fake.reply
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for number, line in enumerate(fake.lines):
                if number == fake.hold_after:
                    self.hold()
                    return
                try:
                    self.wfile.write(line.encode() + b"\n\n")
                    self.wfile.flush()
                except OSError:
                    # The client ended the stream early, as it does after a failed chunk.
                    return

        def hold(self):
            fake.release.wait(5)
            self.connection.settimeout(1)
            try:
                fake.peer_closed = self.connection.recv(1, socket.MSG_PEEK) == b""
            except TimeoutError:
                fake.peer_closed = False
            except OSError:
                fake.peer_closed = True
            fake.handled.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    fake.provider = CodysseyGptProvider(GptSettings(KEY, base_url, APPROVED_MODEL))
    yield fake
    fake.release.set()
    server.shutdown()
    server.server_close()


def read(gpt, timeout_seconds=5.0):
    """The pieces received before the stream ended, and the failure that ended it, if any."""
    pieces = []
    try:
        for piece in gpt.provider.stream(CONTEXT, timeout_seconds=timeout_seconds):
            pieces.append(piece)
    except (ProviderFailed, ProviderTimeout) as error:
        return pieces, error
    return pieces, None


def assert_nothing_private(text):
    for private in PRIVATE:
        assert private not in text


def test_pieces_arrive_in_order_from_one_streaming_request_with_the_fixed_model(gpt, caplog):
    caplog.set_level(logging.INFO)
    assert read(gpt) == (["합성 ", "답변 조각"], None)
    (made,) = gpt.requests
    assert made.path == "/v1/chat/completions"
    assert made.headers["Authorization"] == f"Bearer {KEY}"
    assert made.body == {
        "model": "gpt-5.5",
        "messages": [{"role": item.role, "content": item.content} for item in CONTEXT],
        "stream": True,
    }
    assert "mode=stream outcome=completed status=200 pieces=2" in caplog.text
    assert_nothing_private(caplog.text)


def test_comments_blank_lines_and_chunks_without_choices_carry_no_piece(gpt):
    usage = "data: " + json.dumps({"choices": [], "usage": {"total_tokens": 3}})
    gpt.lines = [": keep-alive", chunk(""), chunk("답변"), "event: other", chunk(finish_reason="stop"), usage, DONE]
    assert read(gpt) == (["답변"], None)


@pytest.mark.parametrize(
    ("lines", "logged"),
    [
        ([chunk("합성 "), chunk(finish_reason="length"), DONE], "finish_reason=length pieces=1"),
        ([chunk("합성 "), chunk(finish_reason="content_filter"), DONE], "finish_reason=content_filter pieces=1"),
        ([chunk("합성 "), DONE], "finish_reason=other pieces=1"),
        ([chunk("합성 "), chunk(finish_reason="fixture-unknown-reason"), DONE], "finish_reason=other pieces=1"),
        # The body ends before `[DONE]`, with or without a stated reason.
        ([chunk("합성 ")], "finish_reason=no_done pieces=1"),
        ([chunk("합성 "), chunk(finish_reason="stop")], "finish_reason=no_done pieces=1"),
    ],
    ids=["length limit", "content filter", "no reason", "unknown reason", "cut off", "stop without done"],
)
def test_a_stream_that_did_not_end_naturally_fails_after_its_pieces(gpt, caplog, lines, logged):
    caplog.set_level(logging.INFO)
    gpt.lines = lines
    pieces, error = read(gpt)
    assert pieces == ["합성 "] and isinstance(error, ProviderFailed)
    assert len(gpt.requests) == 1
    assert f"outcome=incomplete {logged}" in caplog.text
    assert "fixture-unknown-reason" not in caplog.text
    assert_nothing_private(caplog.text)


@pytest.mark.parametrize(
    ("line", "logged"),
    [
        ("data: " + json.dumps({"error": {"message": "fixture-provider-detail"}}), "outcome=provider_error"),
        ("data: not json", "outcome=invalid_response"),
        ("data: [1, 2]", "outcome=invalid_response"),
    ],
    ids=["error chunk", "not json", "not an object"],
)
def test_an_error_or_malformed_chunk_fails_the_stream(gpt, caplog, line, logged):
    caplog.set_level(logging.INFO)
    gpt.lines = [chunk("합성 "), line, chunk(finish_reason="stop"), DONE]
    pieces, error = read(gpt)
    assert pieces == ["합성 "] and isinstance(error, ProviderFailed)
    assert logged in caplog.text
    assert_nothing_private(caplog.text)


@pytest.mark.parametrize(
    "reply",
    [(429, b'{"error": {"message": "rate limit"}}'), (429, b'{"error": {"code": "insufficient_quota"}}'), (500, b"x")],
    ids=["429", "quota", "500"],
)
def test_a_refused_request_fails_before_any_piece_with_no_retry_or_other_model(gpt, caplog, reply):
    caplog.set_level(logging.INFO)
    gpt.reply = reply
    pieces, error = read(gpt)
    assert pieces == [] and isinstance(error, ProviderFailed)
    assert len(gpt.requests) == 1 and gpt.requests[0].body["model"] == "gpt-5.5"
    assert f"outcome=http_error status={reply[0]}" in caplog.text
    assert "rate limit" not in caplog.text and "insufficient_quota" not in caplog.text


def test_the_deadline_covers_the_whole_stream_and_cancels_the_connection(gpt):
    gpt.hold_after = 1
    started = time.monotonic()
    pieces, error = read(gpt, timeout_seconds=0.3)
    assert pieces == ["합성 "] and isinstance(error, ProviderTimeout)
    assert time.monotonic() - started < 1.5
    gpt.release.set()
    assert gpt.handled.wait(5) and gpt.peer_closed is True
    assert len(gpt.requests) == 1


def test_pieces_still_buffered_after_the_deadline_end_the_stream_as_a_timeout(gpt, caplog):
    """The server sent everything, natural end included, but the reader fell behind the deadline."""
    caplog.set_level(logging.INFO)
    pieces = gpt.provider.stream(CONTEXT, timeout_seconds=0.3)
    assert next(pieces) == "합성 "
    # By now the remaining piece, the stop reason and `[DONE]` wait in the buffer.
    time.sleep(0.5)
    with pytest.raises(ProviderTimeout):
        next(pieces)
    assert "mode=stream outcome=timeout" in caplog.text
    assert "outcome=completed" not in caplog.text
    assert len(gpt.requests) == 1


def test_a_caller_that_stops_reading_cancels_the_request(gpt):
    gpt.hold_after = 1
    pieces = gpt.provider.stream(CONTEXT, timeout_seconds=5)
    assert next(pieces) == "합성 "
    pieces.close()  # type: ignore[attr-defined]
    gpt.release.set()
    assert gpt.handled.wait(5) and gpt.peer_closed is True


def test_a_stream_that_is_never_read_sends_nothing(gpt):
    gpt.provider.stream(CONTEXT, timeout_seconds=5)
    time.sleep(0.3)
    assert gpt.requests == []


# The adapter in the queue runner: what each end of the stream becomes.


def test_the_runner_stores_a_natural_end_as_completed_and_forwards_each_piece(gpt):
    seen = []
    outcome = chat_runner.generate(gpt.provider, CONTEXT, on_delta=seen.append)
    assert outcome == Outcome("completed", text="합성 답변 조각")
    assert seen == ["합성 ", "답변 조각"] and len(gpt.requests) == 1


@pytest.mark.parametrize(
    "lines",
    [[chunk("합성 "), chunk(finish_reason="length"), DONE], [chunk("합성 ")]],
    ids=["length limit", "cut off"],
)
def test_the_runner_never_stores_a_partial_answer(gpt, lines):
    gpt.lines = lines
    seen = []
    # The pieces were shown as they arrived, but the stored outcome is a failure without text.
    assert chat_runner.generate(gpt.provider, CONTEXT, on_delta=seen.append) == chat_queue.PROVIDER_FAILED
    assert seen == ["합성 "] and len(gpt.requests) == 1


def test_the_runner_fails_a_refused_request_without_calling_again(gpt):
    gpt.reply = (429, b'{"error": {"code": "insufficient_quota"}}')
    assert chat_runner.generate(gpt.provider, CONTEXT) == chat_queue.PROVIDER_FAILED
    assert len(gpt.requests) == 1


def test_the_runner_leaves_a_stream_past_the_time_limit_unknown(gpt):
    gpt.hold_after = 1
    started = time.monotonic()
    assert chat_runner.generate(gpt.provider, CONTEXT, time_limit=0.3) == chat_queue.OUTCOME_UNKNOWN
    assert time.monotonic() - started < 1.5
    gpt.release.set()
    assert gpt.handled.wait(5) and gpt.peer_closed is True
    assert len(gpt.requests) == 1


class Sessions:
    @contextmanager
    def __call__(self):
        yield self

    def commit(self):
        pass


def test_one_job_runs_the_real_adapter_once_and_stores_its_outcome(gpt, monkeypatch):
    token = uuid.uuid4()
    finished = []
    monkeypatch.setattr(
        chat_queue, "claim_next", lambda session, ledger, user_id: (Claimed(7, token, 70, CONTEXT), Changes([7]))
    )

    def finish(session, ledger, job_id, run_token, outcome):
        finished.append((job_id, run_token, outcome))
        return Changes([job_id])

    monkeypatch.setattr(chat_queue, "finish", finish)
    deltas = []
    assert chat_runner.run_next(Sessions(), gpt.provider, object(), 101, on_delta=lambda *a: deltas.append(a))
    assert finished == [(7, token, Outcome("completed", text="합성 답변 조각"))]
    assert deltas == [(7, "합성 "), (7, "답변 조각")]
    assert len(gpt.requests) == 1
