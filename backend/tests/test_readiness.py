"""Readiness parts without a database: schema comparison, the read-only check and the worker's status (#72)."""

import threading
import time
from unittest.mock import MagicMock, patch

import psycopg

from app import readiness
from app.services.chat_runner import ChatJobWorker
from app.services.quota import MemberQuota

PRODUCT_TABLES = {
    "users",
    "auth_identities",
    "refresh_sessions",
    "conversations",
    "messages",
    "chat_quotas",
    "chat_quota_reservations",
    "chat_queue_members",
    "chat_jobs",
    "chat_conversation_holds",
}


def test_expected_columns_are_the_ten_product_tables_only():
    expected = readiness.expected_columns()
    # conftest registers a probe model on the same metadata; it is not a product table.
    assert set(expected) == PRODUCT_TABLES
    assert {"id", "run_token"} <= expected["chat_jobs"]


def test_compare_schema_reports_missing_tables_before_columns():
    expected = {"a": frozenset({"id", "name"}), "b": frozenset({"id"})}
    assert readiness.compare_schema(expected, {"a": {"id", "name"}, "b": {"id"}}) == ("ok", ())
    assert readiness.compare_schema(expected, {"a": {"id"}}) == ("missing_tables", ("b",))
    assert readiness.compare_schema(expected, {"a": {"id"}, "b": {"id"}}) == ("missing_columns", ("a.name",))
    # Extra tables and columns are allowed: an older app version can still run on a newer schema.
    assert readiness.compare_schema(expected, {"a": {"id", "name", "x"}, "b": {"id"}, "c": {"id"}}) == ("ok", ())


def test_check_opens_a_read_only_bounded_connection(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db:5432/app")
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    columns = [(table, column) for table, wanted in readiness.expected_columns().items() for column in wanted]
    cursor.fetchall.return_value = columns
    with patch("app.readiness.psycopg.connect", return_value=connection) as connect:
        assert readiness.check_database() == ("ok", "ok", (), None)
    assert connect.call_args.kwargs == {
        "connect_timeout": 2,
        "options": "-c statement_timeout=2000 -c default_transaction_read_only=on",
    }
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements[0] == "SELECT 1"
    assert statements[1].startswith("SELECT table_name, column_name FROM information_schema.columns")


def test_schema_query_failure_keeps_database_ok(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db:5432/app")
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    cursor.execute.side_effect = [None, psycopg.errors.QueryCanceled("SELECT secret")]
    with patch("app.readiness.psycopg.connect", return_value=connection):
        database, schema, missing, error = readiness.check_database()
    assert (database, schema, missing) == ("ok", "unknown", ())
    assert error is not None and "secret" not in str(error)


def test_unexpected_select_result_is_unavailable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db:5432/app")
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (2,)
    with patch("app.readiness.psycopg.connect", return_value=connection):
        assert readiness.check_database()[:2] == ("unavailable", "unknown")


def worker() -> ChatJobWorker:
    return ChatJobWorker(MagicMock(), MagicMock(), MemberQuota(), poll_seconds=0.01)


def wait_for(condition, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_worker_status_through_start_poll_stall_and_stop():
    subject = worker()
    assert subject.status(stale_after=30) == "not_started"
    with patch.object(subject, "tick", return_value=[]):
        subject.start()
        wait_for(lambda: subject.status(stale_after=30) == "ok")
        subject._last_poll = time.monotonic() - 60
        assert subject.status(stale_after=30) == "stalled"
        wait_for(lambda: subject.status(stale_after=30) == "ok")
        subject.stop(timeout=2)
    assert subject.status(stale_after=30) == "stopped"


def test_worker_is_starting_until_a_poll_succeeds_and_recovers_after():
    subject = worker()
    healthy = threading.Event()

    def tick() -> list[int]:
        if not healthy.is_set():
            raise psycopg.OperationalError("down")
        return []

    with patch.object(subject, "tick", side_effect=tick):
        subject.start()
        time.sleep(0.05)
        assert subject.status(stale_after=30) == "starting"
        healthy.set()
        wait_for(lambda: subject.status(stale_after=30) == "ok")
        subject.stop(timeout=2)


def test_worker_whose_poll_thread_died_is_stopped():
    subject = worker()
    subject._loop = threading.Thread(target=lambda: None)
    subject._loop.start()
    subject._loop.join()
    assert subject.status(stale_after=30) == "stopped"
