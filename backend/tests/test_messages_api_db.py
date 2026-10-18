"""Sending a question against real PostgreSQL rows, with a stand-in provider.

Authentication is mocked as in test_conversations_api_db.py, and no test calls a
provider outside the test. Every user and message is fake. All tests share the one
transaction of the test connection, so the database clock does not advance; an old
answer is made by storing an old creation time.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.integrations.provider import ProviderFailed, ProviderTimeout, get_provider
from app.main import app
from app.models.conversation import Conversation, Message
from app.models.user import User
from app.repositories import conversations as repository
from app.services.chat import STALE_PENDING_SECONDS
from test_conversations_api import call, error_of, failing, sign_in
from test_messages_api import Crash, FakeProvider

pytestmark = pytest.mark.db

OLD = datetime(2026, 1, 2, 3, 6, tzinfo=UTC)
DONE = [("user", "completed", "안녕?", None), ("assistant", "completed", "안녕하세요.", None)]
WAITING = [("user", "completed", "안녕?", None), ("assistant", "pending", None, None)]
INTERRUPTED = [("user", "completed", "안녕?", None), ("assistant", "interrupted", None, "outcome_unknown")]


@pytest.fixture
def session(orm_test_connection):
    """One session for seeding and for the route, so the route's commits stay inside the rolled-back test."""
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:
        app.dependency_overrides[get_session] = lambda: session
        yield session
        app.dependency_overrides.clear()


def save(session, row):
    session.add(row)
    session.flush()
    return row.id


@pytest.fixture
def seeded(session, auth_user_a, auth_user_b):
    """The two shared fixture users with one empty conversation each; the first user is signed in."""
    user_a = save(session, User(email=auth_user_a["register_input"]["email"], password_hash="fake-hash"))
    user_b = save(session, User(email=auth_user_b["register_input"]["email"], password_hash="fake-hash"))
    mine = save(session, Conversation(user_id=user_a, created_at=OLD, updated_at=OLD))
    theirs = save(session, Conversation(user_id=user_b))
    session.commit()
    sign_in(user_a)
    return SimpleNamespace(user_a=user_a, user_b=user_b, mine=mine, theirs=theirs)


@pytest.fixture
def provider(session):
    """Observes whether the request session has a transaction open during each provider call."""
    fake = FakeProvider(session.in_transaction)
    app.dependency_overrides[get_provider] = lambda: fake
    return fake


def send(conversation_id, content="안녕?", request_key="q-demo-1"):
    body = {"content": content, "request_key": request_key}
    return call("POST", f"/api/conversations/{conversation_id}/messages", json=body)


def stored(session, conversation_id):
    """The rows as the database holds them, not as the session remembers them."""
    statement = (
        select(Message.role, Message.status, Message.content, Message.error_code)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)
    )
    return [tuple(row) for row in session.execute(statement)]


def make_pending_old(session, conversation_id):
    session.execute(update(Message).where(Message.conversation_id == conversation_id).values(created_at=OLD))
    session.commit()


def test_answer_is_stored_and_read_back_after_the_request(session, seeded, provider):
    response = send(seeded.mine)
    assert response.status_code == 201
    question, answer = response.json()["question"], response.json()["answer"]
    assert (question["role"], question["status"], question["content"], question["request_key"]) == (
        "user",
        "completed",
        "안녕?",
        "q-demo-1",
    )
    assert question["completed_at"] == question["created_at"]
    assert (answer["role"], answer["status"], answer["content"], answer["attempt_no"]) == (
        "assistant",
        "completed",
        "안녕하세요.",
        1,
    )
    assert answer["reply_to_message_id"] == question["id"] and answer["completed_at"] is not None
    assert stored(session, seeded.mine) == DONE
    # The provider was called once, with no transaction open.
    (made,) = provider.calls
    assert made.observed is False and made.context == [("user", "안녕?")]
    # A reload reads the same pair from the history.
    history = call("GET", f"/api/conversations/{seeded.mine}/messages")
    assert history.json() == {"items": [question, answer], "next_cursor": None}
    assert session.scalar(select(Conversation.updated_at).where(Conversation.id == seeded.mine)) > OLD


def test_resend_returns_the_stored_pair_and_another_body_is_409(session, seeded, provider):
    first = send(seeded.mine)
    again = send(seeded.mine)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    conflict = send(seeded.mine, content="안녕!")
    assert conflict.status_code == 409 and error_of(conflict)["code"] == "request_key_conflict"
    assert stored(session, seeded.mine) == DONE and len(provider.calls) == 1
    # The same key is a different question in another user's conversation.
    sign_in(seeded.user_b)
    assert send(seeded.theirs, content="다른 사용자의 질문이야.").status_code == 201


def test_question_and_pending_answer_are_stored_together_or_not_at_all(session, seeded, provider, monkeypatch):
    """The pending answer of a second question breaks the one-pending rule, and its question goes with it."""
    provider.outcome = failing(Crash())
    with pytest.raises(Crash):
        send(seeded.mine)
    monkeypatch.setattr(repository, "has_pending_answer", lambda *_args: False)
    with pytest.raises(IntegrityError):
        send(seeded.mine, content="하나 더", request_key="q-demo-2")
    session.rollback()
    assert stored(session, seeded.mine) == WAITING


@pytest.mark.parametrize(
    ("outcome", "status_code", "code", "ended"),
    [
        (failing(ProviderFailed()), 502, "provider_failed", ("assistant", "failed", None, "provider_failed")),
        (failing(ProviderTimeout()), 504, "provider_timeout", ("assistant", "interrupted", None, "outcome_unknown")),
    ],
    ids=["failure", "timeout"],
)
def test_failure_is_stored_and_left_out_of_the_next_context(
    session, seeded, provider, outcome, status_code, code, ended
):
    provider.outcome = outcome
    response = send(seeded.mine)
    assert response.status_code == status_code and error_of(response)["code"] == code
    assert stored(session, seeded.mine) == [DONE[0], ended]
    again = send(seeded.mine)
    assert again.status_code == 200
    answer = again.json()["answer"]
    assert (answer["role"], answer["status"], answer["content"], answer["error_code"]) == ended
    assert answer["completed_at"] is None and len(provider.calls) == 1
    # The conversation is not blocked, and the unfinished pair reaches the provider on neither side.
    provider.outcome = lambda: "두 번째 답변"
    assert send(seeded.mine, content="하나 더", request_key="q-demo-2").status_code == 201
    assert provider.calls[-1].context == [("user", "하나 더")]
    context = repository.list_context_messages(session, seeded.user_a, seeded.mine)
    assert [(row.role, row.content) for row in context] == [("user", "하나 더"), ("assistant", "두 번째 답변")]
    # The history keeps both pairs.
    assert stored(session, seeded.mine) == [
        DONE[0],
        ended,
        ("user", "completed", "하나 더", None),
        ("assistant", "completed", "두 번째 답변", None),
    ]


def test_after_a_crash_the_answer_stays_pending_until_the_recovery_limit(session, seeded, provider):
    provider.outcome = failing(Crash())
    with pytest.raises(Crash):
        send(seeded.mine)
    assert stored(session, seeded.mine) == WAITING
    provider.outcome = lambda: "안녕하세요."
    waiting = send(seeded.mine)
    assert waiting.status_code == 202
    answer = waiting.json()["answer"]
    assert (answer["status"], answer["content"], answer["completed_at"]) == ("pending", None, None)
    busy = send(seeded.mine, content="하나 더", request_key="q-demo-2")
    assert busy.status_code == 409 and error_of(busy)["code"] == "conversation_busy"
    assert stored(session, seeded.mine) == WAITING and len(provider.calls) == 1

    make_pending_old(session, seeded.mine)
    recovered = send(seeded.mine)
    assert recovered.status_code == 200
    answer = recovered.json()["answer"]
    assert (answer["status"], answer["content"], answer["error_code"]) == ("interrupted", None, "outcome_unknown")
    assert stored(session, seeded.mine) == INTERRUPTED and len(provider.calls) == 1
    history = call("GET", f"/api/conversations/{seeded.mine}/messages").json()["items"]
    assert history == [recovered.json()["question"], answer]


def test_a_new_question_closes_an_old_pending_answer_and_is_answered(session, seeded, provider):
    provider.outcome = failing(Crash())
    with pytest.raises(Crash):
        send(seeded.mine)
    make_pending_old(session, seeded.mine)
    provider.outcome = lambda: "안녕하세요."
    assert send(seeded.mine, content="하나 더", request_key="q-demo-2").status_code == 201
    assert stored(session, seeded.mine) == [
        *INTERRUPTED,
        ("user", "completed", "하나 더", None),
        ("assistant", "completed", "안녕하세요.", None),
    ]
    assert provider.calls[-1].context == [("user", "하나 더")]


def test_recovery_closes_only_answers_older_than_the_limit(session, seeded, provider):
    """Exactly at the limit is not yet stale; one second more is. The database clock is fixed in the test."""
    provider.outcome = failing(Crash())
    with pytest.raises(Crash):
        send(seeded.mine)
    for seconds, closed, expected in ((STALE_PENDING_SECONDS, 0, WAITING), (STALE_PENDING_SECONDS + 1, 1, INTERRUPTED)):
        session.execute(
            update(Message)
            .where(Message.conversation_id == seeded.mine)
            .values(created_at=func.now() - timedelta(seconds=seconds))
        )
        assert repository.close_stale_pending(session, seeded.mine, STALE_PENDING_SECONDS) == closed
        assert stored(session, seeded.mine) == expected


def test_reading_the_history_does_not_recover_an_old_pending_answer(session, seeded, provider):
    """Only a message POST recovers; a GET reads what is stored and changes nothing."""
    provider.outcome = failing(Crash())
    with pytest.raises(Crash):
        send(seeded.mine)
    make_pending_old(session, seeded.mine)
    history = call("GET", f"/api/conversations/{seeded.mine}/messages")
    assert history.status_code == 200
    answer = history.json()["items"][1]
    assert (answer["status"], answer["error_code"]) == ("pending", None)
    assert stored(session, seeded.mine) == WAITING


def test_recovery_leaves_recent_and_ended_answers_alone(session, seeded, provider):
    assert send(seeded.mine).status_code == 201
    make_pending_old(session, seeded.mine)
    repository.close_stale_pending(session, seeded.mine, STALE_PENDING_SECONDS)
    session.commit()
    assert stored(session, seeded.mine) == DONE


def test_an_answer_that_arrives_after_recovery_closed_it_is_not_stored(session, seeded, provider):
    def recovered_meanwhile():
        make_pending_old(session, seeded.mine)
        repository.close_stale_pending(session, seeded.mine, STALE_PENDING_SECONDS)
        session.commit()
        return "늦은 답변"

    provider.outcome = recovered_meanwhile
    response = send(seeded.mine)
    assert response.status_code == 504 and error_of(response)["code"] == "provider_timeout"
    assert stored(session, seeded.mine) == INTERRUPTED


def test_another_users_conversation_is_404_and_stores_nothing(session, seeded, provider):
    others = send(seeded.theirs)
    missing = send(seeded.theirs + 1000)
    assert others.status_code == missing.status_code == 404
    assert error_of(others) == error_of(missing) == {"code": "not_found", "message": "찾을 수 없습니다.", "fields": {}}
    assert session.scalar(select(func.count()).select_from(Message)) == 0 and provider.calls == []


def retry(conversation_id, question_id, retry_key="r-demo-1"):
    return call(
        "POST", f"/api/conversations/{conversation_id}/messages/{question_id}/retry", json={"retry_key": retry_key}
    )


def stored_question_id(session, conversation_id):
    """The conversation's latest question."""
    statement = select(Message.id).where(Message.conversation_id == conversation_id, Message.role == "user")
    return session.scalars(statement.order_by(Message.id.desc()).limit(1)).one()


def attempts(session, question_id):
    statement = (
        select(Message.attempt_no, Message.retry_key, Message.status, Message.content)
        .where(Message.reply_to_message_id == question_id)
        .order_by(Message.attempt_no)
    )
    return [tuple(row) for row in session.execute(statement)]


@pytest.mark.parametrize(
    ("outcome", "first"),
    [(failing(ProviderFailed()), "failed"), (failing(ProviderTimeout()), "interrupted")],
    ids=["failed", "interrupted"],
)
def test_retry_stores_a_new_attempt_once_per_key(session, seeded, provider, outcome, first):
    provider.outcome = outcome
    send(seeded.mine)
    question_id = stored_question_id(session, seeded.mine)
    provider.outcome = lambda: "다시 만든 답변"
    response = retry(seeded.mine, question_id)
    assert response.status_code == 201
    answer = response.json()["answer"]
    assert (answer["attempt_no"], answer["status"], answer["content"]) == (2, "completed", "다시 만든 답변")
    # Called with no transaction open, and with the question only: the failed attempt is not context.
    assert provider.calls[-1].observed is False and provider.calls[-1].context == [("user", "안녕?")]
    again = retry(seeded.mine, question_id)
    assert again.status_code == 200 and again.json() == response.json()
    assert attempts(session, question_id) == [(1, None, first, None), (2, "r-demo-1", "completed", "다시 만든 답변")]
    assert len(provider.calls) == 2
    history = call("GET", f"/api/conversations/{seeded.mine}/messages").json()["items"]
    assert [item["attempt_no"] for item in history] == [None, 1, 2]
    # The answer is completed now, so another key is refused.
    refused = retry(seeded.mine, question_id, retry_key="r-demo-2")
    assert refused.status_code == 409 and error_of(refused)["code"] == "retry_not_allowed"


def test_retry_refuses_completed_older_and_foreign_questions(session, seeded, provider):
    assert send(seeded.mine).status_code == 201
    completed_id = stored_question_id(session, seeded.mine)
    refused = retry(seeded.mine, completed_id)
    assert refused.status_code == 409 and error_of(refused)["code"] == "retry_not_allowed"

    provider.outcome = failing(ProviderFailed())
    send(seeded.mine, content="하나 더", request_key="q-demo-2")
    failed_id = stored_question_id(session, seeded.mine)
    provider.outcome = lambda: "세 번째 답변"
    send(seeded.mine, content="셋째", request_key="q-demo-3")
    older = retry(seeded.mine, failed_id)
    assert older.status_code == 409 and error_of(older)["code"] == "retry_not_latest"

    sign_in(seeded.user_b)
    assert retry(seeded.mine, failed_id).status_code == 404
    assert attempts(session, failed_id) == [(1, None, "failed", None)]


def test_a_late_result_of_the_first_attempt_does_not_overwrite_the_retry(session, seeded, provider):
    def retried_meanwhile():
        make_pending_old(session, seeded.mine)
        provider.outcome = lambda: "다시 만든 답변"
        assert retry(seeded.mine, stored_question_id(session, seeded.mine)).status_code == 201
        return "늦은 답변"

    provider.outcome = retried_meanwhile
    response = send(seeded.mine)
    assert response.status_code == 504
    question_id = stored_question_id(session, seeded.mine)
    assert attempts(session, question_id) == [
        (1, None, "interrupted", None),
        (2, "r-demo-1", "completed", "다시 만든 답변"),
    ]
