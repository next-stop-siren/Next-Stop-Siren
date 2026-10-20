"""Unit checks for running one chat job without a database (#52).

The provider is a fake; no network call is made. The queue service is replaced, so
these checks only cover what the runner does between the two transactions.
"""

import threading
import time
import uuid
from contextlib import contextmanager

from app.integrations.provider import ContextMessage, ProviderFailed, ProviderTimeout
from app.services import chat_queue, chat_runner
from app.services.chat_queue import Changes, Claimed, Outcome

CONTEXT = [ContextMessage("user", "질문")]


class Provider:
    def __init__(self, pieces=(), error=None, pause=0.0):
        self.pieces = pieces
        self.error = error
        self.pause = pause
        self.calls = 0

    def complete(self, messages, *, timeout_seconds):
        raise AssertionError("the runner streams")

    def stream(self, messages, *, timeout_seconds):
        self.calls += 1
        for piece in self.pieces:
            if self.pause:
                time.sleep(self.pause)
            yield piece
        if self.error is not None:
            raise self.error


def test_pieces_join_into_a_completed_answer_and_reach_on_delta():
    seen = []
    outcome = chat_runner.generate(Provider(("모의 ", "답변")), CONTEXT, on_delta=seen.append)
    assert outcome == Outcome("completed", text="모의 답변")
    assert seen == ["모의 ", "답변"]


def test_blank_answer_and_provider_failure_are_failed():
    assert chat_runner.generate(Provider((" ", "\n")), CONTEXT) == chat_queue.PROVIDER_FAILED
    assert chat_runner.generate(Provider(("조각",), ProviderFailed()), CONTEXT) == chat_queue.PROVIDER_FAILED


def test_timeout_and_unexpected_errors_leave_the_outcome_unknown():
    assert chat_runner.generate(Provider((), ProviderTimeout()), CONTEXT) == chat_queue.OUTCOME_UNKNOWN
    assert chat_runner.generate(Provider((), RuntimeError("boom")), CONTEXT) == chat_queue.OUTCOME_UNKNOWN


def test_a_provider_that_stops_sending_is_cut_off_at_the_time_limit():
    started = time.monotonic()
    outcome = chat_runner.generate(Provider(("늦은", "조각"), pause=1.0), CONTEXT, time_limit=0.2)
    assert outcome == chat_queue.OUTCOME_UNKNOWN
    assert time.monotonic() - started < 0.9


class Sessions:
    """Counts open fake sessions so a provider call can check that none is open."""

    def __init__(self):
        self.open = 0
        self.lock = threading.Lock()

    @contextmanager
    def __call__(self):
        with self.lock:
            self.open += 1
        try:
            yield self
        finally:
            with self.lock:
                self.open -= 1

    def commit(self):
        pass


def test_no_session_is_open_while_the_provider_runs(monkeypatch):
    sessions = Sessions()
    token = uuid.uuid4()
    finished = []
    open_during_call = []

    class CheckingProvider(Provider):
        def stream(self, messages, *, timeout_seconds):
            open_during_call.append(sessions.open)
            yield "답변"

    monkeypatch.setattr(
        chat_queue, "claim_next", lambda session, ledger, user_id: (Claimed(7, token, 70, CONTEXT), Changes([7]))
    )

    def finish(session, ledger, job_id, run_token, outcome):
        finished.append((job_id, run_token, outcome))
        return Changes([job_id])

    monkeypatch.setattr(chat_queue, "finish", finish)
    notified = []
    deltas = []
    ran = chat_runner.run_next(
        sessions, CheckingProvider(), object(), 101, notify=notified.append, on_delta=lambda *a: deltas.append(a)
    )

    assert ran is True
    assert open_during_call == [0]
    assert finished == [(7, token, Outcome("completed", text="답변"))]
    assert notified == [[7], [7]]
    assert deltas == [(7, "답변")]


def test_nothing_runs_when_the_execution_right_is_taken(monkeypatch):
    provider = Provider(("답변",))
    monkeypatch.setattr(chat_queue, "claim_next", lambda session, ledger, user_id: (None, Changes()))
    assert chat_runner.run_next(Sessions(), provider, object(), 101) is False
    assert provider.calls == 0
