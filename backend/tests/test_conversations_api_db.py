"""The conversation API reading and writing real PostgreSQL rows.

Authentication is mocked: the two users are stored with the emails of the shared
authentication fixtures, and a test injects the generated ID of one of them as the
already verified user. No test here performs a login or verifies a token. Every
user and message is fake.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import db
from app.db import get_session
from app.main import app
from app.models.conversation import Conversation, Message
from app.models.user import User
from test_conversations_api import call, error_of, ids, sign_in

pytestmark = pytest.mark.db

NOW = datetime(2026, 1, 2, 3, 6, tzinfo=UTC)


@pytest.fixture
def session(orm_test_connection):
    """One session for seeding and for the routes, so a route's commit stays inside the rolled-back test."""
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:
        app.dependency_overrides[get_session] = lambda: session
        yield session
        app.dependency_overrides.clear()


def save(session, row):
    session.add(row)
    session.flush()
    return row.id


def add_exchange(session, conversation_id, request_key, content, failed=False):
    """A question and its first answer; returns both IDs."""
    question = save(
        session,
        Message(
            conversation_id=conversation_id,
            role="user",
            status="completed",
            content=content,
            request_key=request_key,
            completed_at=NOW,
        ),
    )
    outcome = (
        dict(status="failed", error_code="provider_failed")
        if failed
        else dict(status="completed", content="안녕하세요.", completed_at=NOW)
    )
    answer = save(
        session,
        Message(
            conversation_id=conversation_id, role="assistant", reply_to_message_id=question, attempt_no=1, **outcome
        ),
    )
    return question, answer


@pytest.fixture
def seeded(session, auth_user_a, auth_user_b):
    """The two shared fixture users; the second user's conversation sits between the first user's."""
    user_a = save(session, User(email=auth_user_a["register_input"]["email"], password_hash="fake-hash"))
    user_b = save(session, User(email=auth_user_b["register_input"]["email"], password_hash="fake-hash"))
    first = save(session, Conversation(user_id=user_a))
    other = save(session, Conversation(user_id=user_b))
    second = save(session, Conversation(user_id=user_a))
    greeting = add_exchange(session, first, "q-demo-1", "안녕?")
    todo = add_exchange(session, first, "q-demo-2", "오늘 할 일을 정리해 줘.", failed=True)
    theirs = add_exchange(session, other, "q-demo-1", "다른 사용자의 질문이야.")
    return SimpleNamespace(
        user_a=user_a,
        user_b=user_b,
        first=first,
        other=other,
        second=second,
        first_messages=[*greeting, *todo],
        other_messages=list(theirs),
    )


def texts(numbers):
    return [str(number) for number in numbers]


def test_created_conversation_is_stored_for_the_verified_user_only(session, seeded):
    sign_in(seeded.user_b)
    response = call("POST", "/api/conversations", json={})
    assert response.status_code == 201
    created = response.json()["conversation"]
    assert set(created) == {"id", "created_at", "updated_at"}
    assert created["created_at"].endswith("Z") and created["created_at"] == created["updated_at"]
    assert session.get(Conversation, int(created["id"])).user_id == seeded.user_b
    assert ids(call("GET", "/api/conversations")) == [created["id"], str(seeded.other)]
    sign_in(seeded.user_a)
    assert ids(call("GET", "/api/conversations")) == texts([seeded.second, seeded.first])


def test_rejected_create_stores_nothing(session, seeded):
    before = session.scalar(select(func.count()).select_from(Conversation))
    assert call("POST", "/api/conversations", json={}).status_code == 401
    sign_in(seeded.user_a)
    assert call("POST", "/api/conversations", json={"user_id": str(seeded.user_b)}).status_code == 422
    assert session.scalar(select(func.count()).select_from(Conversation)) == before


class CommitFails(Session):
    """A request session whose commit meets a database failure after the row was written."""

    rows_before_commit = None

    def commit(self) -> None:
        type(self).rows_before_commit = self.scalar(select(func.count()).select_from(Conversation))
        raise OperationalError("COMMIT", {}, Exception("server closed the connection, password=fixture-secret"))


def test_failed_save_answers_503_and_leaves_no_conversation(session, seeded, orm_test_connection, monkeypatch):
    """The real request session dependency runs here, so its rollback is what removes the row."""
    session.commit()
    before = session.scalar(select(func.count()).select_from(Conversation))
    session.commit()
    del app.dependency_overrides[get_session]
    monkeypatch.setattr(
        db, "_session_factory", lambda: CommitFails(orm_test_connection, join_transaction_mode="create_savepoint")
    )
    sign_in(seeded.user_a)
    response = call("POST", "/api/conversations", json={})
    assert response.status_code == 503
    assert error_of(response) == {
        "code": "temporarily_unavailable",
        "message": "잠시 후 다시 시도해 주세요.",
        "fields": {},
    }
    assert "conversation" not in response.json() and "fixture-secret" not in response.text
    # The row existed inside the failed request and is gone after it.
    assert CommitFails.rows_before_commit == before + 1
    assert session.scalar(select(func.count()).select_from(Conversation)) == before


def test_list_pages_through_my_conversations_without_gaps_or_repeats(session, seeded):
    sign_in(seeded.user_a)
    first_page = call("GET", "/api/conversations?limit=1").json()
    assert [item["id"] for item in first_page["items"]] == [str(seeded.second)]
    assert first_page["next_cursor"] == str(seeded.second)
    second_page = call("GET", f"/api/conversations?limit=1&before_id={first_page['next_cursor']}").json()
    assert [item["id"] for item in second_page["items"]] == [str(seeded.first)]
    assert second_page["next_cursor"] is None


def test_history_reads_back_completed_and_failed_answers(session, seeded):
    sign_in(seeded.user_a)
    response = call("GET", f"/api/conversations/{seeded.first}/messages")
    assert response.status_code == 200
    assert ids(response) == texts(seeded.first_messages)
    question, answer, _, failed = response.json()["items"]
    assert (question["role"], question["content"], question["request_key"]) == ("user", "안녕?", "q-demo-1")
    assert (answer["status"], answer["content"], answer["attempt_no"]) == ("completed", "안녕하세요.", 1)
    assert answer["reply_to_message_id"] == question["id"]
    assert answer["completed_at"] == "2026-01-02T03:06:00Z"
    assert (failed["status"], failed["content"], failed["error_code"], failed["completed_at"]) == (
        "failed",
        None,
        "provider_failed",
        None,
    )
    page = call("GET", f"/api/conversations/{seeded.first}/messages?limit=3").json()
    assert page["next_cursor"] == str(seeded.first_messages[2])
    rest = call("GET", f"/api/conversations/{seeded.first}/messages?limit=3&after_id={page['next_cursor']}")
    assert ids(rest) == texts(seeded.first_messages[3:])
    assert call("GET", f"/api/conversations/{seeded.second}/messages").json() == {"items": [], "next_cursor": None}


def test_another_users_conversation_and_a_missing_one_are_the_same_404(session, seeded):
    sign_in(seeded.user_b)
    others = call("GET", f"/api/conversations/{seeded.first}/messages")
    missing = call("GET", f"/api/conversations/{seeded.second + 1000}/messages")
    assert others.status_code == missing.status_code == 404
    assert error_of(others) == error_of(missing) == {"code": "not_found", "message": "찾을 수 없습니다.", "fields": {}}
    assert ids(call("GET", f"/api/conversations/{seeded.other}/messages")) == texts(seeded.other_messages)
    sign_in(seeded.user_a)
    assert call("GET", f"/api/conversations/{seeded.other}/messages").status_code == 404
