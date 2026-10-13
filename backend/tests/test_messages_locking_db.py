"""Recovery and answer saving at the same time, each on its own database connection.

Two connections cannot see each other's uncommitted rows, so unlike the other
database tests these rows are committed. The fixture deletes them afterwards and
drops only the tables it created. Every user and message is fake.
"""

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, event, inspect, select, text
from sqlalchemy.orm import Session

from app.db import Base, psycopg_url
from app.models import register_models
from app.models.conversation import Conversation, Message
from app.models.user import User
from app.repositories import conversations as repository
from app.services.chat import STALE_PENDING_SECONDS
from db_guard import guarded_live_url

pytestmark = pytest.mark.db

OLD = datetime(2026, 1, 2, 3, 6, tzinfo=UTC)
WAIT_SECONDS = 10


@pytest.fixture
def committed():
    """A committed conversation whose only answer is pending and older than the recovery limit."""
    url = guarded_live_url()
    # A lock wait that outlasts the test fails it instead of hanging it.
    engine = create_engine(psycopg_url(url), connect_args={"connect_timeout": 3, "options": "-c lock_timeout=15000"})
    register_models()
    existing = set(inspect(engine).get_table_names())
    created = [table for table in Base.metadata.sorted_tables if table.name not in existing]
    Base.metadata.create_all(engine, tables=created)
    rows = SimpleNamespace(engine=engine, user=None, conversation=None)
    try:
        with Session(engine) as session:
            user = User(email="locking@example.test", password_hash="fake-hash")
            session.add(user)
            session.flush()
            rows.user = user.id
            conversation = Conversation(user_id=user.id)
            session.add(conversation)
            session.flush()
            rows.conversation = conversation.id
            question = Message(
                conversation_id=conversation.id,
                role="user",
                status="completed",
                content="안녕?",
                request_key="q-demo-1",
                created_at=OLD,
                completed_at=OLD,
            )
            session.add(question)
            session.flush()
            answer = Message(
                conversation_id=conversation.id,
                role="assistant",
                status="pending",
                reply_to_message_id=question.id,
                attempt_no=1,
                created_at=OLD,
            )
            session.add(answer)
            session.flush()
            rows.answer = answer.id
            session.commit()
        yield rows
    finally:
        with engine.begin() as connection:
            if rows.conversation is not None:
                # Answers first: they reference their questions.
                for role in ("assistant", "user"):
                    connection.execute(
                        delete(Message).where(Message.conversation_id == rows.conversation, Message.role == role)
                    )
                connection.execute(delete(Conversation).where(Conversation.id == rows.conversation))
            if rows.user is not None:
                connection.execute(delete(User).where(User.id == rows.user))
        Base.metadata.drop_all(engine, tables=created)
        engine.dispose()


def test_recovery_waits_for_an_answer_being_saved_instead_of_deadlocking(committed):
    """Saving pauses right after its first lock until recovery is waiting on a lock, then goes on.

    With opposite lock orders the two would now wait for each other and PostgreSQL
    would abort one of them as a deadlock.
    """
    engine = committed.engine
    saving_connection, recovering_connection = engine.connect(), engine.connect()
    recovering_pid = recovering_connection.scalar(text("SELECT pg_backend_pid()"))
    recovering_connection.commit()
    saving_holds_a_lock, recovery_waited = threading.Event(), threading.Event()
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def pause_after_first_statement(*_args) -> None:
        if saving_holds_a_lock.is_set():
            return
        saving_holds_a_lock.set()
        with engine.connect() as watcher:
            deadline = time.monotonic() + WAIT_SECONDS
            while time.monotonic() < deadline:
                state = watcher.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": recovering_pid}
                )
                # Activity statistics are a snapshot per transaction.
                watcher.rollback()
                if state == "Lock":
                    recovery_waited.set()
                    return
                time.sleep(0.05)

    def save() -> None:
        try:
            with Session(saving_connection) as session:
                answer = repository.finish_answer(
                    session,
                    committed.conversation,
                    committed.answer,
                    status="completed",
                    content="늦게 도착한 답변",
                    error_code=None,
                )
                results["saved"] = answer.status
                session.commit()
        except BaseException as error:
            errors.append(error)

    def recover() -> None:
        try:
            assert saving_holds_a_lock.wait(WAIT_SECONDS)
            with Session(recovering_connection) as session:
                assert repository.lock_conversation(session, committed.user, committed.conversation)
                results["closed"] = repository.close_stale_pending(
                    session, committed.conversation, STALE_PENDING_SECONDS
                )
                session.commit()
        except BaseException as error:
            errors.append(error)

    event.listen(saving_connection, "after_cursor_execute", pause_after_first_statement)
    try:
        threads = [threading.Thread(target=save), threading.Thread(target=recover)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3 * WAIT_SECONDS)
        assert not any(thread.is_alive() for thread in threads)
    finally:
        event.remove(saving_connection, "after_cursor_execute", pause_after_first_statement)
        saving_connection.close()
        recovering_connection.close()

    assert errors == []
    assert recovery_waited.is_set()
    # Saving held the conversation, so recovery ran after it and found nothing left to close.
    assert results == {"saved": "completed", "closed": 0}
    with Session(engine) as session:
        answer = session.scalars(select(Message).where(Message.id == committed.answer)).one()
        assert (answer.status, answer.content, answer.error_code) == ("completed", "늦게 도착한 답변", None)
