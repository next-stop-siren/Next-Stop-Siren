"""Sending a question with a streamed answer, compared with the completed answer of test_messages_api.py.

The storage stand-in, the fake rows and the mocked authentication are those of
test_messages_api.py. The provider is a stand-in that yields set pieces and makes no
call outside the test. What is checked here is the boundary between the pieces, which
are temporary output, and the stored answer.
"""

import asyncio
import json
import logging
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from app.db import get_session, get_session_factory
from app.integrations.provider import MockProvider, ProviderFailed, ProviderTimeout, get_provider
from app.main import app
from app.repositories import conversations as repository
from app.services import chat
from app.services.chat import ANSWER_TIME_LIMIT_SECONDS, STALE_PENDING_SECONDS
from test_conversations_api import call, error_of, failing, sign_in
from test_messages_api import ANSWER, FAILED, INTERRUPTED, PENDING, QUESTION, STALE, FakeChatStore

JSON_PATH = "/api/conversations/501/messages"
PATH = f"{JSON_PATH}/stream"
ASKED = ("user", "completed", "안녕?", None)
# Private text a request and its answer carry; the server log may not repeat it.
PRIVATE_PIECES = ["fixture-private-piece-1 ", "fixture-private-piece-2"]


class StreamProvider:
    """Yields the set pieces, then runs `ending`, which raises to fail the stream after them."""

    def __init__(self, observe) -> None:
        self.observe = observe
        self.pieces = ["안녕", "하세요."]
        self.before_piece = lambda index: None
        self.ending = lambda: None
        self.calls: list[SimpleNamespace] = []

    def complete(self, messages, *, timeout_seconds):
        """The same answer or failure as one whole, for comparing the two routes."""
        return "".join(self.stream(messages, timeout_seconds=timeout_seconds))

    def stream(self, messages, *, timeout_seconds):
        context = [(item.role, item.content) for item in messages]
        self.calls.append(SimpleNamespace(context=context, observed=self.observe(), timeout_seconds=timeout_seconds))
        for index, piece in enumerate(self.pieces):
            self.before_piece(index)
            yield piece
        self.ending()


@pytest.fixture
def store(monkeypatch, auth_user_a):
    fake = FakeChatStore(auth_user_a["principal"]["user_id"])
    for name in vars(FakeChatStore):
        if not name.startswith("_") and hasattr(repository, name):
            monkeypatch.setattr(repository, name, getattr(fake, name))
    fake.opened = 0
    # The stream route closes the request session before calling the provider.
    fake.closed = 0

    def close():
        fake.closed += 1

    fake.close = close

    def open_session():
        fake.opened += 1
        return nullcontext(fake)

    app.dependency_overrides[get_session] = lambda: fake
    # The session a stream opens for its last transaction; here the same stand-in, counted.
    app.dependency_overrides[get_session_factory] = lambda: open_session
    sign_in(fake.owner)
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def provider(store):
    """Observes how many commits were made before the provider was called."""
    fake = StreamProvider(lambda: store.commits)
    app.dependency_overrides[get_provider] = lambda: fake
    return fake


def send(content="안녕?", request_key="q-demo-1", path=PATH):
    return call("POST", path, json={"content": content, "request_key": request_key})


def stored(store):
    return [(row.role, row.status, row.content, row.error_code) for row in store.rows]


def parse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.removesuffix("\n\n").split("\n\n"):
        name, data = block.split("\n")
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def events_of(response: httpx.Response) -> list[tuple[str, dict]]:
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    return parse_events(response.text)


def error_event(events) -> dict:
    """The data of a last `error` event without its trace ID, as `error_of` gives a JSON error."""
    name, data = events[-1]
    assert name == "error"
    error = dict(data["error"])
    assert isinstance(error.pop("trace_id"), str)
    return error


def test_new_question_is_streamed_and_done_carries_the_stored_answer(store, provider):
    response = send()
    assert events_of(response) == [
        ("start", {"question": QUESTION, "answer": PENDING}),
        ("delta", {"text": "안녕"}),
        ("delta", {"text": "하세요."}),
        ("done", {"question": QUESTION, "answer": ANSWER}),
    ]
    assert (response.headers["cache-control"], response.headers["x-accel-buffering"]) == ("no-cache", "no")
    assert stored(store) == [ASKED, ("assistant", "completed", "안녕하세요.", None)]
    # One call, made after the question and its pending answer were committed; the answer
    # was committed in a session of its own.
    (made,) = provider.calls
    assert made.observed == 1 and (store.commits, store.opened, store.closed) == (2, 1, 1)
    assert made.context == [("user", "안녕?")]
    assert made.timeout_seconds == ANSWER_TIME_LIMIT_SECONDS < STALE_PENDING_SECONDS


def test_a_streamed_answer_is_stored_and_reported_as_the_completed_answer_is(store, provider):
    completed = send(path=JSON_PATH)
    rows = stored(store)
    store.rows.clear()
    events = events_of(send())
    assert completed.status_code == 201
    assert events[-1] == ("done", completed.json())
    assert stored(store) == rows
    # Either route reads the stored pair back with the same key, whichever route stored it.
    assert send().json() == send(path=JSON_PATH).json() == completed.json()


@pytest.mark.parametrize("path", [PATH, JSON_PATH], ids=["stream route", "JSON route"])
def test_resending_the_key_returns_the_stored_pair_as_json_without_a_second_call(store, provider, path):
    send()
    again = send(path=path)
    assert again.status_code == 200
    assert again.headers["content-type"] == "application/json"
    assert again.json() == {"question": QUESTION, "answer": ANSWER}
    assert len(store.rows) == 2 and len(provider.calls) == 1


@pytest.mark.parametrize(
    ("pieces", "ending", "code", "answer"),
    [
        (["안녕"], failing(ProviderFailed()), "provider_failed", FAILED),
        (["안녕"], failing(ProviderTimeout()), "provider_timeout", INTERRUPTED),
        ([], failing(ProviderFailed()), "provider_failed", FAILED),
        ([" ", "\n"], lambda: None, "provider_failed", FAILED),
    ],
    ids=["failure after a piece", "timeout after a piece", "failure before any piece", "only whitespace"],
)
def test_a_stream_that_fails_stores_no_text_and_ends_with_the_json_routes_error(
    store, provider, pieces, ending, code, answer
):
    provider.pieces, provider.ending = pieces, ending
    events = events_of(send())
    assert [name for name, _data in events] == ["start", *["delta"] * len(pieces), "error"]
    # The same error body the JSON route sends with 502 or 504.
    failed = send(content="하나 더", request_key="q-demo-2", path=JSON_PATH)
    assert error_event(events) == error_of(failed)
    assert error_event(events)["code"] == code
    assert stored(store)[1] == ("assistant", answer["status"], None, answer["error_code"])
    again = send()
    assert again.status_code == 200
    assert again.json() == {"question": QUESTION, "answer": answer}
    assert len(provider.calls) == 2


def test_pieces_of_a_failed_stream_do_not_enter_the_next_context(store, provider):
    send(content="하나", request_key="q-1")
    provider.pieces, provider.ending = ["버려질 ", "조각"], failing(ProviderFailed())
    send(content="둘", request_key="q-2")
    provider.ending = failing(ProviderTimeout())
    send(content="셋", request_key="q-3")
    provider.pieces, provider.ending = ["네 번째 답변"], lambda: None
    assert events_of(send(content="넷", request_key="q-4"))[-1][0] == "done"
    # A failed or interrupted pair leaves the context whole, its question too.
    assert provider.calls[-1].context == [("user", "하나"), ("assistant", "안녕하세요."), ("user", "넷")]
    assert [row.content for row in store.rows if row.role == "assistant"] == ["안녕하세요.", None, None, "네 번째 답변"]


def test_when_the_answer_cannot_be_saved_no_done_is_sent_and_the_answer_stays_pending(
    store, provider, monkeypatch, caplog
):
    statement = "UPDATE messages SET content=%(content)s"
    failure = OperationalError(statement, {"content": "".join(PRIVATE_PIECES)}, Exception("password=fixture-secret"))
    provider.pieces = PRIVATE_PIECES
    monkeypatch.setattr(repository, "finish_answer", failing(failure))
    with caplog.at_level(logging.INFO, logger="app"):
        events = events_of(send())
    # Every piece was sent, but nothing says the answer is complete.
    assert [name for name, _data in events] == ["start", "delta", "delta", "error"]
    assert error_event(events) == {
        "code": "temporarily_unavailable",
        "message": "잠시 후 다시 시도해 주세요.",
        "fields": {},
    }
    assert stored(store) == [ASKED, ("assistant", "pending", None, None)]
    # The error event leads to the log entries of this request, and the log repeats no text.
    trace_id = events[-1][1]["error"]["trace_id"]
    messages = [record.getMessage() for record in caplog.records]
    assert [message.split(",")[0] for message in messages] == [
        "Question received",
        "Question saved",
        "Provider call ended",
        "Database unavailable",
    ]
    assert all(f"trace_id={trace_id}" in message for message in messages)
    for private in (*PRIVATE_PIECES, statement, "fixture-secret"):
        assert private not in caplog.text
    # The screen asks again with the same key and reads what is stored.
    monkeypatch.setattr(repository, "finish_answer", store.finish_answer)
    again = send()
    assert again.status_code == 202
    assert again.json() == {"question": QUESTION, "answer": PENDING}
    assert len(provider.calls) == 1


def test_a_stream_logs_under_one_trace_id_and_never_logs_a_piece(store, provider, caplog):
    provider.pieces, provider.ending = PRIVATE_PIECES, failing(ProviderFailed("fixture-provider-secret"))
    with caplog.at_level(logging.INFO, logger="app"):
        events = events_of(send())
    trace_id = events[-1][1]["error"]["trace_id"]
    messages = [record.getMessage() for record in caplog.records]
    assert [message.split(",")[0] for message in messages] == [
        "Question received",
        "Question saved",
        "Provider call ended",
        "Answer saved",
    ]
    assert all(f"trace_id={trace_id}" in message for message in messages)
    for private in (*PRIVATE_PIECES, "fixture-provider-secret"):
        assert private not in caplog.text


def test_an_answer_that_ends_after_recovery_closed_it_is_not_stored(store, provider):
    def recovered_meanwhile():
        store.rows[1].created_at = STALE
        store.close_stale_pending(None, 501, STALE_PENDING_SECONDS)

    provider.ending = recovered_meanwhile
    events = events_of(send())
    assert [name for name, _data in events] == ["start", "delta", "delta", "error"]
    assert error_event(events)["code"] == "provider_timeout"
    assert stored(store)[1] == ("assistant", "interrupted", None, "outcome_unknown")


# Where the client leaves, and what the provider holds until it has: the next piece, or its end.
LEAVING = {
    "before the first piece": ("start", 0),
    "between two pieces": ("delta", 1),
    "after the last piece": ("delta", None),
}


def leave_after(provider, leaving: str, path: str = PATH) -> list[dict]:
    """Send the question as an ASGI client that disconnects at the `LEAVING` point named.

    The provider holds what follows until the client has left, so the stream is always
    abandoned before the provider ends. After the last piece the provider then ends
    normally, a moment later: the case where it ends while the server still waits for it.
    Returns the messages the application sent.
    """
    event, held = LEAVING[leaving]
    left = threading.Event()
    if held is None:
        provider.pieces = ["안녕하세요."]
        provider.ending = lambda: left.wait(5) and time.sleep(0.3)
    else:
        provider.pieces = ["안녕", "하세요", "."]
        provider.before_piece = lambda index: index == held and left.wait(5)
    marker = f"event: {event}".encode()
    body = json.dumps({"content": "안녕?", "request_key": "q-demo-1"}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def run() -> list[dict]:
        sent: list[dict] = []
        written = asyncio.Event()
        requests = iter([{"type": "http.request", "body": body, "more_body": False}])

        async def receive() -> dict:
            for request in requests:
                return request
            await written.wait()
            if held is None:
                # Let the server reach its wait for the provider's end before the client leaves.
                await asyncio.sleep(0.1)
            left.set()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)
            if marker in message.get("body", b""):
                written.set()

        await app(scope, receive, send)
        return sent

    return asyncio.run(run())


def written_events(sent: list[dict]) -> list[str]:
    assert sent[0]["status"] == 200
    return [name for name, _data in parse_events(b"".join(message.get("body", b"") for message in sent[1:]).decode())]


@pytest.mark.parametrize("leaving", list(LEAVING))
def test_a_reader_that_leaves_gets_no_done_and_the_answer_is_stored_as_interrupted(store, provider, caplog, leaving):
    with caplog.at_level(logging.WARNING, logger="app"):
        names = written_events(leave_after(provider, leaving))
    assert "Stream abandoned" in caplog.text
    assert names[0] == "start" and "done" not in names
    # No piece is stored, although some were sent.
    assert stored(store) == [ASKED, ("assistant", "interrupted", None, "outcome_unknown")]
    # The screen asks again with the same key: the stored state, without another call.
    # After `start` the reader may leave before the provider is even called.
    calls = len(provider.calls)
    again = send()
    assert again.status_code == 200
    assert again.json() == {"question": QUESTION, "answer": INTERRUPTED}
    assert len(provider.calls) == calls <= 1
    # The conversation is not blocked, and the abandoned pieces are not in the next context.
    provider.before_piece = provider.ending = lambda *_args: None
    assert events_of(send(content="하나 더", request_key="q-demo-2"))[-1][0] == "done"
    assert provider.calls[-1].context == [("user", "하나 더")]


def test_closing_the_service_stream_interrupts_an_unfinished_answer_at_any_point(store, provider):
    def stream(request_key):
        started = chat.begin_question(store, store.owner, 501, request_key, request_key, trace_id="t")
        return chat.AnswerStream(lambda: nullcontext(store), provider, started, trace_id="t")

    unread = stream("q-1")
    unread.close()
    assert stored(store)[1] == ("assistant", "interrupted", None, "outcome_unknown")
    # Closed before it was read, so the provider was never called.
    assert provider.calls == []

    abandoned = stream("q-2")
    assert next(abandoned) == "안녕"
    abandoned.close()
    assert stored(store)[3] == ("assistant", "interrupted", None, "outcome_unknown")

    finished = stream("q-3")
    *pieces, answer = finished
    finished.close()
    assert pieces == ["안녕", "하세요."] and answer is store.rows[5]
    assert stored(store)[5] == ("assistant", "completed", "안녕하세요.", None)


def test_closing_while_next_waits_for_the_end_never_stores_completed(store, provider):
    """`close` from another thread wakes a `next` that waits for the provider's end, even if it ends later."""
    released = threading.Event()
    provider.pieces = ["안녕하세요."]
    provider.ending = lambda: released.wait(5)
    started = chat.begin_question(store, store.owner, 501, "안녕?", "q-demo-1", trace_id="t")
    stream = chat.AnswerStream(lambda: nullcontext(store), provider, started, trace_id="t")
    assert next(stream) == "안녕하세요."
    waited: list = []
    reader = threading.Thread(target=lambda: waited.append(next(stream)))
    reader.start()
    time.sleep(0.1)
    assert reader.is_alive()
    stream.close()
    reader.join(1)
    assert not reader.is_alive()
    # The provider ends only now, after the reader left: the stored answer is still interrupted.
    released.set()
    time.sleep(0.1)
    (answer,) = waited
    assert answer.status == "interrupted"
    assert stored(store)[1] == ("assistant", "interrupted", None, "outcome_unknown")


# Short enough to wait out in a test; the stalls below are longer.
SHORT_LIMIT = 0.2
STALL = 0.6


@pytest.mark.parametrize(
    ("stall_at", "delta_count"),
    [(0, 0), (1, 1), (2, 2)],
    ids=["before the first piece", "between two pieces", "after the last piece"],
)
def test_a_stream_past_the_time_limit_is_interrupted_without_done(store, provider, monkeypatch, stall_at, delta_count):
    """The provider really stalls; the stream ends at the limit, not when the provider resumes."""
    monkeypatch.setattr(chat, "ANSWER_TIME_LIMIT_SECONDS", SHORT_LIMIT)
    provider.before_piece = lambda index: index == stall_at and time.sleep(STALL)
    provider.ending = lambda: stall_at == 2 and time.sleep(STALL)
    began = time.monotonic()
    events = events_of(send())
    took = time.monotonic() - began
    assert [name for name, _data in events] == ["start", *["delta"] * delta_count, "error"]
    assert error_event(events)["code"] == "provider_timeout"
    assert stored(store)[1] == ("assistant", "interrupted", None, "outcome_unknown")
    assert SHORT_LIMIT <= took < STALL
    # The screen reads the stored state, and the next question is answered.
    assert send().json() == {"question": QUESTION, "answer": INTERRUPTED}
    provider.before_piece = provider.ending = lambda *_args: None
    assert events_of(send(content="하나 더", request_key="q-demo-2"))[-1][0] == "done"


def test_a_piece_that_arrives_just_in_time_still_completes(store, provider, monkeypatch):
    monkeypatch.setattr(chat, "ANSWER_TIME_LIMIT_SECONDS", STALL * 2)
    provider.before_piece = lambda index: index == 1 and time.sleep(SHORT_LIMIT)
    assert events_of(send())[-1] == ("done", {"question": QUESTION, "answer": ANSWER})


def test_what_the_json_route_refuses_is_refused_as_json_before_a_stream_starts(store, provider, auth_user_b):
    store.add_question(None, 501, "안녕?", "q-demo-1")
    waiting = send()
    refusals = [
        (send(content="안녕!"), 409, "request_key_conflict"),
        (send(content="하나 더", request_key="q-demo-2"), 409, "conversation_busy"),
        (send(content=" "), 422, "validation_error"),
        (send(path="/api/conversations/999/messages/stream"), 404, "not_found"),
    ]
    sign_in(auth_user_b["principal"]["user_id"])
    refusals.append((send(), 404, "not_found"))
    assert waiting.status_code == 202
    assert waiting.json() == {"question": QUESTION, "answer": PENDING}
    for response, status_code, code in refusals:
        assert response.status_code == status_code
        assert response.headers["content-type"] == "application/json"
        assert error_of(response)["code"] == code
    assert len(store.rows) == 2 and provider.calls == [] and store.opened == 0


def test_unauthenticated_stream_gets_401_before_anything_else(store, provider, unauthenticated_case):
    app.dependency_overrides.clear()
    body = {"content": "안녕?", "request_key": "q-demo-1"}
    response = call("POST", PATH, headers=unauthenticated_case["request_headers"], json=body)
    assert response.status_code == unauthenticated_case["expected_status"]
    assert error_of(response)["code"] == "unauthenticated"
    assert (store.calls, store.commits, provider.calls) == (0, 0, [])


def test_the_mock_streams_the_answer_it_completes():
    mock = MockProvider()
    pieces = list(mock.stream([], timeout_seconds=ANSWER_TIME_LIMIT_SECONDS))
    assert len(pieces) > 1
    assert "".join(pieces) == mock.complete([], timeout_seconds=ANSWER_TIME_LIMIT_SECONDS)


def test_openapi_describes_the_stream_responses():
    operation = app.openapi()["paths"]["/api/conversations/{conversation_id}/messages/stream"]["post"]
    assert set(operation["responses"]) == {"200", "202", "401", "404", "409", "422", "503"}
    pair = {"$ref": "#/components/schemas/QuestionAnswer"}
    assert operation["responses"]["200"]["content"] == {
        "text/event-stream": {},
        "application/json": {"schema": pair},
    }
    assert operation["responses"]["202"]["content"] == {"application/json": {"schema": pair}}
