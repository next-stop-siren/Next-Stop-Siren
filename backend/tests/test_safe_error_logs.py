"""Failure logs of the chat runner, its notifications and the server keep no sensitive values (#70).

Each synthetic error carries marker strings where a real one would carry the SQL
statement, its bound values, the question, an access token and the connection string.
The errors go through the real `except` blocks, and the check reads what a log handler
writes (`caplog.text`, which includes any attached traceback). No real user text, key
or database is used.
"""

import asyncio
import logging
import threading
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from sqlalchemy.exc import DataError, IntegrityError, OperationalError

from app.api.chat_events import ChatEvents, Subscription
from app.integrations.provider import ContextMessage, ProviderFailed
from app.observability import SafeError, SafeExceptionLogFilter, protect_exception_logs
from app.services import chat_queue, chat_runner, chat_views
from app.services.chat_queue import Changes, Claimed

SQL_MARK = "secret_sql_marker_table"
QUESTION_MARK = "question-marker-70"
ANSWER_MARK = "answer-marker-70"
TOKEN_MARK = "eyJ-token-marker-70"
CONNECTION_MARK = "postgresql://app:password-marker-70@db-marker.internal/app"
MARKS = (SQL_MARK, QUESTION_MARK, ANSWER_MARK, TOKEN_MARK, "password-marker-70", "db-marker.internal")

STATEMENT = f"UPDATE {SQL_MARK} SET content = %(content)s WHERE token = %(token)s"
VALUES = {"content": QUESTION_MARK, "token": TOKEN_MARK}


def database_error(kind=DataError, orig: Exception | None = None):
    """A SQLAlchemy error whose text holds the statement, the bound values and the connection string."""
    return kind(STATEMENT, VALUES, orig or Exception(f"server refused {QUESTION_MARK} at {CONNECTION_MARK}"))


def assert_safe(caplog) -> None:
    assert caplog.records, "the failure must still be logged"
    assert all(record.exc_info is None and record.exc_text is None for record in caplog.records)
    assert not any(mark in caplog.text for mark in MARKS)


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
        yield ANSWER_MARK


def worker(sessions=None) -> chat_runner.ChatJobWorker:
    return chat_runner.ChatJobWorker(sessions or Sessions(), Provider(), object())  # type: ignore[arg-type]


def test_a_database_error_while_claiming_is_logged_as_a_cause_without_its_text(monkeypatch, caplog):
    def claim_next(session, ledger, user_id):
        raise database_error()

    monkeypatch.setattr(chat_queue, "claim_next", claim_next)
    with caplog.at_level(logging.INFO):
        worker()._drain(101)
    assert_safe(caplog)
    stopped, failed = (record.getMessage() for record in caplog.records)
    # Nothing started yet, so no job ID; the stage says the claim did not commit.
    assert stopped.startswith(
        "Job run stopped, user_id=101, job_id=None, stage=claim, cause=database_error, error_type=DataError/Exception"
    )
    assert failed.startswith("Chat job run failed, user_id=101, cause=database_error, error_type=DataError/Exception")
    assert "where=services/chat_runner.py:" in failed


def test_an_outcome_that_was_not_stored_is_never_logged_as_stored(monkeypatch, caplog):
    """The answer reached the runner but its commit failed: the job is named, the answer is not."""
    token = uuid.uuid4()
    monkeypatch.setattr(
        chat_queue,
        "claim_next",
        lambda session, ledger, user_id: (Claimed(7, token, 70, [ContextMessage("user", QUESTION_MARK)]), Changes()),
    )

    def finish(session, ledger, job_id, run_token, outcome):
        raise database_error(OperationalError)

    monkeypatch.setattr(chat_queue, "finish", finish)
    with caplog.at_level(logging.INFO):
        worker()._drain(101)
    assert_safe(caplog)
    messages = [record.getMessage() for record in caplog.records]
    assert "Job started, job_id=7, answer_id=70" in messages
    assert any(text.startswith("Provider call ended, job_id=7, outcome=completed") for text in messages)
    (stored,) = [text for text in messages if text.startswith("Job outcome not stored")]
    assert stored.startswith(
        "Job outcome not stored, job_id=7, outcome=completed, cause=database_unavailable, "
        "error_type=OperationalError/Exception, where=services/chat_runner.py:"
    )
    assert messages[-1].startswith("Chat job run failed, user_id=101, cause=database_unavailable")


def test_an_unexpected_error_keeps_only_its_class(monkeypatch, caplog):
    def claim_next(session, ledger, user_id):
        raise RuntimeError(f"bearer {TOKEN_MARK} for {QUESTION_MARK}")

    monkeypatch.setattr(chat_queue, "claim_next", claim_next)
    with caplog.at_level(logging.INFO):
        worker()._drain(101)
    assert_safe(caplog)
    assert "cause=unexpected, error_type=RuntimeError" in caplog.text


def test_a_failed_poll_is_logged_without_its_text(monkeypatch, caplog):
    def members_with_work(session):
        raise database_error()

    monkeypatch.setattr(chat_queue, "members_with_work", members_with_work)
    poller = worker()

    class OnePass:
        """Stands for the wake event: the loop stops after its first tick."""

        def wait(self, _timeout):
            poller._stopping.set()

        def clear(self):
            pass

    poller._wake = OnePass()  # type: ignore[assignment]
    with caplog.at_level(logging.INFO):
        poller._run()
    assert_safe(caplog)
    assert "Chat job poll failed, cause=database_error, error_type=DataError/Exception" in caplog.text


def test_failed_notifications_keep_no_sql_values(monkeypatch, caplog):
    loop = asyncio.new_event_loop()
    try:

        def related_ids(*_args):
            raise database_error(IntegrityError, psycopg.errors.UniqueViolation(f"duplicate {QUESTION_MARK}"))

        monkeypatch.setattr(chat_views, "related_ids", related_ids)
        events = ChatEvents()
        # A subscriber added without its snapshot read; the publish is what fails.
        events._subscribers[101] = {Subscription(101, loop)}

        def no_session():
            raise database_error(OperationalError)

        events.attach(no_session, object())  # type: ignore[arg-type]
        with caplog.at_level(logging.INFO):
            events.publish(Sessions(), 101, [7003])  # type: ignore[arg-type]
            events._subscribers[101] = {Subscription(101, loop)}
            events.notify(101, [7003])
    finally:
        loop.close()
    assert_safe(caplog)
    assert "Chat event publish failed, user_id=101, job_ids=7003, cause=database_conflict" in caplog.text
    assert "error_type=IntegrityError/UniqueViolation/23505" in caplog.text
    assert "Chat event session failed, user_id=101, job_ids=7003, cause=database_unavailable" in caplog.text
    assert "Chat events published" not in caplog.text


class FailingClose:
    """A provider stream whose `close()` raises with the question, a token and the connection string in its text."""

    def __init__(self, error: Exception | None = None):
        self._pieces = iter([ANSWER_MARK])
        self._error = error

    def __iter__(self):
        return self

    def __next__(self):
        piece = next(self._pieces, None)
        if piece is None:
            if self._error is not None:
                raise self._error
            raise StopIteration
        return piece

    def close(self):
        raise RuntimeError(f"closing {QUESTION_MARK} with bearer {TOKEN_MARK} at {CONNECTION_MARK}")


def wait_for_stream_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == chat_runner.STREAM_THREAD_NAME:
            thread.join(timeout=5)


@pytest.mark.parametrize(
    ("stream_error", "expected"),
    [(None, "completed"), (ProviderFailed(), "failed")],
    ids=["after-the-last-piece", "after-a-provider-failure"],
)
def test_a_stream_close_failure_keeps_the_outcome_and_prints_nothing_to_stderr(
    monkeypatch, caplog, capsys, stream_error, expected
):
    """`close()` runs on the helper thread; an exception left there would go to stderr with its traceback."""
    escaped = []
    monkeypatch.setattr(threading, "excepthook", escaped.append)

    class ClosingProvider(Provider):
        def stream(self, messages, *, timeout_seconds):
            return FailingClose(stream_error)

    with caplog.at_level(logging.INFO):
        outcome = chat_runner.generate(ClosingProvider(), [ContextMessage("user", QUESTION_MARK)], job_id=7)
        wait_for_stream_threads()
    assert outcome.status == expected
    assert escaped == []
    captured = capsys.readouterr()
    assert not any(mark in captured.err or mark in captured.out for mark in MARKS)
    assert "Traceback" not in captured.err
    assert_safe(caplog)
    (closed,) = [record.getMessage() for record in caplog.records if "close failed" in record.getMessage()]
    assert closed.startswith("Provider stream close failed, job_id=7, cause=unexpected, error_type=RuntimeError")
    assert "where=services/chat_runner.py:" in closed


def test_an_unexpected_provider_error_is_logged_with_its_job_and_without_its_text(caplog):
    class BrokenProvider(Provider):
        def stream(self, messages, *, timeout_seconds):
            raise RuntimeError(f"bearer {TOKEN_MARK} for {QUESTION_MARK}")

    with caplog.at_level(logging.INFO):
        outcome = chat_runner.generate(BrokenProvider(), [ContextMessage("user", QUESTION_MARK)], job_id=7)
    assert outcome == chat_queue.OUTCOME_UNKNOWN
    assert_safe(caplog)
    assert "Provider raised an unexpected error, job_id=7, cause=unexpected, error_type=RuntimeError" in caplog.text


def test_the_server_log_of_an_unhandled_request_error_keeps_only_the_cause(caplog):
    """Uvicorn logs an exception that escaped the app with its traceback; the filter replaces it."""
    protect_exception_logs()
    protect_exception_logs()
    server = logging.getLogger("uvicorn.error")
    assert sum(isinstance(item, SafeExceptionLogFilter) for item in server.filters) == 1
    try:
        raise database_error(IntegrityError)
    except IntegrityError as error:
        with caplog.at_level(logging.INFO):
            server.error("Exception in ASGI application\n", exc_info=error)
            server.info("Started server process [%d]", 1)
    assert_safe(caplog)
    first, second = (record.getMessage() for record in caplog.records)
    assert first.startswith("Exception in ASGI application cause=database_conflict, error_type=IntegrityError")
    assert "where=" in first
    assert second == "Started server process [1]"


def test_safe_error_names_the_innermost_app_frame_only():
    error = database_error()
    assert SafeError.of(error) == SafeError("database_error", "DataError/Exception", "unknown")
    assert str(SafeError.of(psycopg.OperationalError("x"))).startswith("cause=database_unavailable")
