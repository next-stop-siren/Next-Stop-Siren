"""Streaming an answer against real PostgreSQL rows, with a stand-in provider.

Authentication is mocked and the provider is the stand-in of test_messages_stream_api.py;
no test calls a provider outside the test. Every user and message is fake. The request
session and the session the stream opens for its last transaction are one test session,
whose commits stay inside the rolled-back test. Like a real session closing, the
stream's session rolls back what it did not commit when its work fails.
"""

import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import get_session, get_session_factory
from app.integrations.provider import get_provider
from app.main import app
from app.models.conversation import Conversation
from app.models.user import User
from app.services import chat
from test_conversations_api import call, sign_in
from test_messages_api_db import DONE, INTERRUPTED, OLD, WAITING, save, stored
from test_messages_stream_api import (
    LEAVING,
    SHORT_LIMIT,
    STALL,
    StreamProvider,
    error_event,
    events_of,
    leave_after,
    written_events,
)

pytestmark = pytest.mark.db


@pytest.fixture
def session(orm_test_connection):
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:

        @contextmanager
        def open_session():
            try:
                yield session
            except BaseException:
                session.rollback()
                raise

        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_session_factory] = lambda: open_session
        yield session
        app.dependency_overrides.clear()


@pytest.fixture
def seeded(session, auth_user_a):
    """The first shared fixture user, signed in, with one empty conversation."""
    user_id = save(session, User(email=auth_user_a["register_input"]["email"], password_hash="fake-hash"))
    mine = save(session, Conversation(user_id=user_id, created_at=OLD, updated_at=OLD))
    session.commit()
    sign_in(user_id)
    return SimpleNamespace(user_id=user_id, mine=mine, path=f"/api/conversations/{mine}/messages/stream")


@pytest.fixture
def provider(session):
    """Observes whether the request session has a transaction open when the provider is called."""
    fake = StreamProvider(session.in_transaction)
    app.dependency_overrides[get_provider] = lambda: fake
    return fake


def send(seeded, content="안녕?", request_key="q-demo-1"):
    return call("POST", seeded.path, json={"content": content, "request_key": request_key})


def test_done_follows_the_commit_and_matches_the_reloaded_history(session, seeded, provider):
    events = events_of(send(seeded))
    assert [name for name, _data in events] == ["start", "delta", "delta", "done"]
    assert stored(session, seeded.mine) == DONE
    # `done` is what the database holds: a reload of the history reads the same pair.
    done = events[-1][1]
    history = call("GET", f"/api/conversations/{seeded.mine}/messages").json()["items"]
    assert history == [done["question"], done["answer"]]
    assert done["answer"]["completed_at"] is not None
    (made,) = provider.calls
    assert made.observed is False and made.context == [("user", "안녕?")]


@pytest.mark.parametrize("leaving", list(LEAVING))
def test_a_reader_that_leaves_leaves_an_interrupted_answer_that_is_read_back(session, seeded, provider, leaving):
    names = written_events(leave_after(provider, leaving, path=seeded.path))
    assert names[0] == "start" and "done" not in names
    assert stored(session, seeded.mine) == INTERRUPTED
    # The same key reads the stored state as JSON, without another provider call.
    calls = len(provider.calls)
    again = send(seeded)
    assert again.status_code == 200
    answer = again.json()["answer"]
    assert (answer["status"], answer["content"], answer["error_code"]) == ("interrupted", None, "outcome_unknown")
    assert len(provider.calls) == calls
    # A new question is answered, and the interrupted pair is not in its context.
    provider.before_piece = provider.ending = lambda *_args: None
    assert events_of(send(seeded, content="하나 더", request_key="q-demo-2"))[-1][0] == "done"
    assert provider.calls[-1].context == [("user", "하나 더")]


def test_when_the_answer_commit_fails_no_done_is_sent_and_the_answer_stays_pending(
    session, seeded, provider, monkeypatch
):
    commits = []
    commit = session.commit

    def second_commit_fails():
        commits.append(True)
        if len(commits) == 2:
            raise OperationalError("COMMIT", {}, Exception("fixture-database-down"))
        commit()

    monkeypatch.setattr(session, "commit", second_commit_fails)
    events = events_of(send(seeded))
    assert [name for name, _data in events] == ["start", "delta", "delta", "error"]
    assert error_event(events)["code"] == "temporarily_unavailable"
    # The completed text was not committed, and closing the stream did not try again.
    assert stored(session, seeded.mine) == WAITING
    again = send(seeded)
    assert again.status_code == 202
    assert again.json()["answer"]["status"] == "pending"
    assert len(provider.calls) == 1


def test_a_stream_past_the_time_limit_is_stored_as_interrupted(session, seeded, provider, monkeypatch):
    monkeypatch.setattr(chat, "ANSWER_TIME_LIMIT_SECONDS", SHORT_LIMIT)
    provider.before_piece = lambda index: index == 1 and time.sleep(STALL)
    events = events_of(send(seeded))
    assert [name for name, _data in events] == ["start", "delta", "error"]
    assert error_event(events)["code"] == "provider_timeout"
    assert stored(session, seeded.mine) == INTERRUPTED
