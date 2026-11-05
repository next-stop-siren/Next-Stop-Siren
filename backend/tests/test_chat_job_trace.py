"""One job ID follows a chat job from its start to its stored outcome, quota and notification (#70).

The queue service and the provider are fakes, so no database or network is used. The
checks read the log lines a handler receives and look for the job ID and, on a failure,
the stage the run stopped at.
"""

import logging
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.api.chat_events import ChatEvents
from app.integrations.provider import ContextMessage
from app.services import chat_queue, chat_runner
from app.services.chat_queue import Changes, Claimed, QuotaResolution

CONTEXT = [ContextMessage("user", "질문")]
TOKEN = uuid.uuid4()


class Sessions:
    @contextmanager
    def __call__(self):
        yield self

    def commit(self):
        pass

    def rollback(self):
        pass


class Provider:
    def complete(self, messages, *, timeout_seconds):
        raise AssertionError("the runner streams")

    def stream(self, messages, *, timeout_seconds):
        yield "답변"


def messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


@pytest.fixture
def queue(monkeypatch):
    """Job 7 starts; storing its outcome settles reservation 70."""
    monkeypatch.setattr(
        chat_queue,
        "claim_next",
        lambda session, ledger, user_id: (Claimed(7, TOKEN, 71, CONTEXT), Changes([7])),
    )
    monkeypatch.setattr(
        chat_queue,
        "finish",
        lambda session, ledger, job_id, run_token, outcome: Changes(
            [job_id], [QuotaResolution(job_id, 70, "completed", "settled")]
        ),
    )


def test_a_completed_job_is_traced_by_its_id_to_the_settle_and_the_notification(queue, caplog):
    notified = []
    with caplog.at_level(logging.INFO):
        assert chat_runner.run_next(Sessions(), Provider(), object(), 101, notify=notified.append)  # type: ignore[arg-type]
    lines = [text for text in messages(caplog) if "job_id=7" in text]
    assert lines[0] == "Job started, job_id=7, answer_id=71"
    assert lines[1].startswith("Provider call ended, job_id=7, outcome=completed")
    assert lines[2:] == [
        "Job outcome stored, job_id=7, outcome=completed, answer_id=71",
        "Quota resolved, job_id=7, state=completed, reservation_id=70, quota=settled",
    ]
    assert notified == [[7], [7]]


def test_a_failed_completion_notice_names_the_job_and_the_stage(queue, caplog):
    calls = []

    def notify(job_ids):
        calls.append(job_ids)
        if len(calls) == 2:
            raise RuntimeError("subscriber gone")

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        chat_runner.run_next(Sessions(), Provider(), object(), 101, notify=notify)  # type: ignore[arg-type]
    # The outcome and its quota were stored before the notice failed.
    assert "Job outcome stored, job_id=7, outcome=completed, answer_id=71" in messages(caplog)
    assert messages(caplog)[-1].startswith(
        "Job run stopped, user_id=101, job_id=7, stage=outcome_notify, cause=unexpected, error_type=RuntimeError"
    )


def test_a_failed_store_is_reported_once_and_no_quota_is_logged(queue, monkeypatch, caplog):
    def finish(session, ledger, job_id, run_token, outcome):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(chat_queue, "finish", finish)
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        chat_runner.run_next(Sessions(), Provider(), object(), 101)  # type: ignore[arg-type]
    lines = messages(caplog)
    assert any(text.startswith("Job outcome not stored, job_id=7, outcome=completed") for text in lines)
    assert not any(text.startswith(("Job run stopped", "Job outcome stored", "Quota resolved")) for text in lines)


def test_quota_of_jobs_ended_while_claiming_is_logged_after_the_claim_commits(monkeypatch, caplog):
    expired = Changes([5], [QuotaResolution(5, 50, "expired", "refunded")])
    monkeypatch.setattr(chat_queue, "claim_next", lambda session, ledger, user_id: (None, expired))
    with caplog.at_level(logging.INFO):
        assert chat_runner.run_next(Sessions(), Provider(), object(), 101) is False  # type: ignore[arg-type]
    assert messages(caplog) == ["Quota resolved, job_id=5, state=expired, reservation_id=50, quota=refunded"]


class Ledger:
    def __init__(self, resolved=True):
        self.resolved = resolved

    def settle(self, session, reservation_id):
        return self.resolved

    def refund(self, session, reservation_id, reason):
        return self.resolved


@pytest.mark.parametrize(
    ("state", "resolved", "result"),
    [("completed", True, "settled"), ("interrupted", True, "refunded"), ("failed", False, "already_resolved")],
)
def test_each_reservation_resolution_is_kept_for_the_log_after_the_commit(state, resolved, result, caplog):
    job = SimpleNamespace(id=7, reservation_id=70, state=state)
    changes = Changes()
    with caplog.at_level(logging.INFO):
        chat_queue._resolve(object(), Ledger(resolved), job, changes)  # type: ignore[arg-type]
        assert not any(text.startswith("Quota resolved") for text in messages(caplog))
        chat_queue.log_quota(changes)
    assert changes.quota == [QuotaResolution(7, 70, state, result)]
    assert f"Quota resolved, job_id=7, state={state}, reservation_id=70, quota={result}" in messages(caplog)


def test_a_notice_without_a_subscriber_says_why_it_was_not_sent(caplog):
    events = ChatEvents()
    with caplog.at_level(logging.INFO):
        events.notify(101, [7])
        events.attach(Sessions(), object())  # type: ignore[arg-type]
        events.notify(101, [7, 8])
        events.publish(Sessions(), 101, [9])  # type: ignore[arg-type]
    assert messages(caplog) == [
        "Chat events not sent, user_id=101, job_ids=7, reason=no_worker",
        "Chat events not sent, user_id=101, job_ids=7,8, reason=no_subscription",
        "Chat events not sent, user_id=101, job_ids=9, reason=no_subscription",
    ]
