"""The Codyssey GPT adapter against a local stand-in server; nothing leaves this machine.

The key, questions and answers are fake. Failures, the 30-second deadline with its
cancellation, and 429 or quota responses are reproduced without spending any quota.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from app.integrations import codyssey
from app.integrations.codyssey import (
    APPROVED_BASE_URL,
    APPROVED_MODEL,
    CodysseyGptProvider,
    GptSettings,
    GptSettingsError,
)
from app.integrations.provider import ContextMessage, MockProvider, ProviderFailed, ProviderTimeout, get_provider
from app.main import app
from app.repositories import conversations as repository
from app.db import get_session
from test_conversations_api import error_of, sign_in
from test_messages_api import REPOSITORY_FUNCTIONS, FakeChatStore, send, stored

KEY = "fixture-codyssey-key"
QUESTION = "합성 질문 fixture-question"
ANSWER = "합성 답변 fixture-answer"
APPROVED = {"CODYSSEY_API_KEY": KEY, "CODYSSEY_BASE_URL": APPROVED_BASE_URL, "CODYSSEY_MODEL": APPROVED_MODEL}


def completion(content=ANSWER, finish_reason="stop"):
    choice = {"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
    if finish_reason is ...:
        del choice["finish_reason"]
    return {"choices": [choice]}


class FakeGpt:
    """Records every request; replies with `reply`, or holds the request open while `hold` is set."""

    def __init__(self) -> None:
        self.requests: list[SimpleNamespace] = []
        self.reply: tuple[int, object] = (200, completion())
        self.hold = False
        self.release = threading.Event()
        self.handled = threading.Event()
        self.peer_closed: bool | None = None


@pytest.fixture
def gpt():
    fake = FakeGpt()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            fake.requests.append(SimpleNamespace(path=self.path, headers=self.headers, body=json.loads(body)))
            if fake.hold:
                fake.release.wait(5)
                # The client sent its whole request, so an empty read means it closed the connection.
                self.connection.settimeout(1)
                try:
                    fake.peer_closed = self.connection.recv(1, socket.MSG_PEEK) == b""
                except TimeoutError:
                    fake.peer_closed = False
                except OSError:
                    fake.peer_closed = True
                fake.handled.set()
                return
            status, reply = fake.reply
            payload = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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


def ask(gpt, timeout_seconds=5.0):
    messages = [
        ContextMessage("user", "이전 질문"),
        ContextMessage("assistant", "이전 답변"),
        ContextMessage("user", QUESTION),
    ]
    return gpt.provider.complete(messages, timeout_seconds=timeout_seconds)


def test_the_approved_settings_are_read_from_the_server_environment():
    settings = GptSettings.from_env(APPROVED)
    assert (settings.base_url, settings.model) == ("https://copa.codyssey.kr/v1", "gpt-5.5")
    assert KEY not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CODYSSEY_API_KEY", ""),
        ("CODYSSEY_API_KEY", f" {KEY}"),
        ("CODYSSEY_BASE_URL", "https://api.openai.com/v1"),
        ("CODYSSEY_BASE_URL", APPROVED_BASE_URL + "/"),
        ("CODYSSEY_MODEL", "gpt-4o"),
        ("CODYSSEY_MODEL", ""),
    ],
)
def test_a_missing_key_or_an_unapproved_url_or_model_is_refused(name, value):
    with pytest.raises(GptSettingsError, match=name) as refused:
        GptSettings.from_env({**APPROVED, name: value})
    assert KEY not in str(refused.value)


def test_one_request_with_the_fixed_model_and_the_given_context(gpt):
    assert ask(gpt) == ANSWER
    (made,) = gpt.requests
    assert made.path == "/v1/chat/completions"
    assert made.headers["Authorization"] == f"Bearer {KEY}"
    assert made.body == {
        "model": "gpt-5.5",
        "messages": [
            {"role": "user", "content": "이전 질문"},
            {"role": "assistant", "content": "이전 답변"},
            {"role": "user", "content": QUESTION},
        ],
    }


@pytest.mark.parametrize(
    "reply",
    [
        (429, {"error": {"message": "rate limit"}}),
        (429, {"error": {"code": "insufficient_quota"}}),
        (401, {"error": {"message": "bad key"}}),
        (404, {"error": {"message": "model not found"}}),
        (500, {"error": {"message": "server error"}}),
        (503, b"unavailable"),
        (200, b"not json"),
        (200, {"choices": []}),
        (200, completion(None)),
    ],
    ids=["429", "quota", "401", "unknown model", "500", "503", "not json", "no choice", "no content"],
)
def test_a_failure_is_one_request_with_no_retry_or_other_model(gpt, reply):
    gpt.reply = reply
    with pytest.raises(ProviderFailed):
        ask(gpt)
    assert len(gpt.requests) == 1 and gpt.requests[0].body["model"] == "gpt-5.5"


@pytest.mark.parametrize(
    ("finish_reason", "logged"),
    [
        ("length", "length"),
        ("content_filter", "content_filter"),
        (None, "other"),
        (..., "other"),
        ("fixture-unknown-reason", "other"),
    ],
    ids=["length limit", "content filter", "null reason", "no reason", "unknown reason"],
)
def test_an_answer_that_did_not_end_naturally_is_a_failure(gpt, caplog, finish_reason, logged):
    caplog.set_level(logging.INFO)
    gpt.reply = (200, completion("잘린 답변", finish_reason))
    with pytest.raises(ProviderFailed):
        ask(gpt)
    assert len(gpt.requests) == 1
    assert f"outcome=incomplete finish_reason={logged}" in caplog.text
    assert "잘린 답변" not in caplog.text and "fixture-unknown-reason" not in caplog.text


def test_an_unreachable_server_is_a_failure():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    provider = CodysseyGptProvider(GptSettings(KEY, f"http://127.0.0.1:{port}/v1", APPROVED_MODEL))
    with pytest.raises(ProviderFailed):
        provider.complete([ContextMessage("user", QUESTION)], timeout_seconds=5)


def test_the_wait_ends_at_the_deadline_and_the_connection_is_cancelled(gpt):
    gpt.hold = True
    started = time.monotonic()
    with pytest.raises(ProviderTimeout):
        ask(gpt, timeout_seconds=0.3)
    assert time.monotonic() - started < 1.5
    gpt.release.set()
    assert gpt.handled.wait(5)
    assert gpt.peer_closed is True
    assert len(gpt.requests) == 1


def test_a_connection_that_opens_after_the_deadline_sends_nothing(gpt, monkeypatch):
    """The connection is still opening at the deadline; once it opens, no request may follow."""
    opening = threading.Event()
    opened = threading.Event()
    closed_after_opening = threading.Event()

    class SlowConnection(codyssey.HTTPConnection):
        def connect(self):
            opening.wait(5)
            super().connect()
            opened.set()

        def close(self):
            if opened.is_set():
                closed_after_opening.set()
            super().close()

    monkeypatch.setattr(codyssey, "HTTPConnection", SlowConnection)
    started = time.monotonic()
    with pytest.raises(ProviderTimeout):
        ask(gpt, timeout_seconds=0.3)
    assert time.monotonic() - started < 1.5
    opening.set()
    assert opened.wait(5) and closed_after_opening.wait(5)
    # Give a stray request time to arrive, then check that none did.
    time.sleep(0.5)
    assert gpt.requests == []


def test_logs_hold_only_status_and_time(gpt, caplog):
    caplog.set_level(logging.INFO)
    ask(gpt)
    gpt.reply = (429, {"error": {"message": "fixture-provider-detail"}})
    with pytest.raises(ProviderFailed):
        ask(gpt)
    assert "outcome=completed status=200" in caplog.text and "status=429" in caplog.text
    for private in (KEY, QUESTION, ANSWER, "fixture-provider-detail", "이전 질문"):
        assert private not in caplog.text


@pytest.fixture
def chat(monkeypatch, auth_user_a, gpt):
    """The send route with in-memory storage and the real adapter pointed at the stand-in server."""
    store = FakeChatStore(auth_user_a["principal"]["user_id"])
    for name in REPOSITORY_FUNCTIONS:
        monkeypatch.setattr(repository, name, getattr(store, name))
    app.dependency_overrides[get_session] = lambda: store
    app.dependency_overrides[get_provider] = lambda: gpt.provider
    sign_in(store.owner)
    yield SimpleNamespace(store=store, gpt=gpt)
    app.dependency_overrides.clear()


def test_through_the_api_an_answer_is_stored_and_the_unfinished_pair_stays_out_of_the_context(chat):
    assert send(content="하나", request_key="q-1").status_code == 201
    chat.gpt.reply = (429, {"error": {"code": "insufficient_quota"}})
    failed = send(content="둘", request_key="q-2")
    assert failed.status_code == 502 and error_of(failed)["code"] == "provider_failed"
    chat.gpt.reply = (200, completion("세 번째 답변"))
    assert send(content="셋", request_key="q-3").status_code == 201
    assert chat.gpt.requests[-1].body["messages"] == [
        {"role": "user", "content": "하나"},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "셋"},
    ]
    answers = [row[1] for row in stored(chat.store) if row[0] == "assistant"]
    assert answers == ["completed", "failed", "completed"]
    # A resend of the failed question shows it without another request.
    assert send(content="둘", request_key="q-2").status_code == 200
    assert len(chat.gpt.requests) == 3


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_through_the_api_a_cut_answer_is_502_and_never_stored_as_completed(chat, finish_reason):
    chat.gpt.reply = (200, completion("잘린 답변", finish_reason))
    response = send()
    assert response.status_code == 502 and error_of(response)["code"] == "provider_failed"
    assert stored(chat.store)[1] == ("assistant", "failed", None, "provider_failed")
    # The cut pair stays out of the next context.
    chat.gpt.reply = (200, completion())
    assert send(content="하나 더", request_key="q-demo-2").status_code == 201
    assert chat.gpt.requests[-1].body["messages"] == [{"role": "user", "content": "하나 더"}]


def test_through_the_api_a_timeout_is_504_and_stored_as_interrupted(chat, monkeypatch):
    monkeypatch.setattr("app.services.chat.ANSWER_TIME_LIMIT_SECONDS", 0.3)
    chat.gpt.hold = True
    response = send()
    assert response.status_code == 504 and error_of(response)["code"] == "provider_timeout"
    assert stored(chat.store)[1] == ("assistant", "interrupted", None, "outcome_unknown")
    assert len(chat.gpt.requests) == 1


def test_the_public_service_still_uses_the_free_mock():
    assert isinstance(get_provider(), MockProvider)
