"""Real PostgreSQL checks for the conversation and message ORM constraints."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import BigInteger, delete, func, insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.db_init import initialize_with_engine
from app.models.conversation import Conversation, Message

pytestmark = pytest.mark.db

FOREIGN_KEY = {"23503", "23001"}  # PostgreSQL 17 reports RESTRICT as 23503; 23001 covers later versions.
UNIQUE = {"23505"}
CHECK = {"23514"}
NOW = datetime(2026, 1, 2, 3, 6, tzinfo=UTC)


@pytest.fixture
def session(orm_test_connection):
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:
        yield session


def users_table():
    """The real users table registered by the authentication models."""
    return Base.metadata.tables["users"]


def add_user(session, email):
    return session.execute(insert(users_table()).values(email=email).returning(users_table().c.id)).scalar_one()


def add_conversation(session, user_id):
    conversation = Conversation(user_id=user_id)
    session.add(conversation)
    session.flush()
    return conversation.id


def question(conversation_id, request_key, **overrides):
    values = dict(role="user", status="completed", content="안녕?", request_key=request_key, completed_at=NOW)
    return Message(conversation_id=conversation_id, **{**values, **overrides})


def answer(conversation_id, reply_to, attempt_no=1, status="completed", **overrides):
    values = dict(role="assistant", status=status, reply_to_message_id=reply_to, attempt_no=attempt_no)
    if status == "completed":
        values.update(content="안녕하세요.", completed_at=NOW)
    elif status in {"failed", "interrupted"}:
        values.update(error_code="provider_failed")
    if attempt_no > 1:
        values.update(retry_key=f"r-demo-{attempt_no}")
    return Message(conversation_id=conversation_id, **{**values, **overrides})


def save(session, row):
    session.add(row)
    session.flush()
    return row.id


def assert_rejected(session, action, states, *constraints):
    """The action fails with the expected constraint and leaves earlier rows in place."""
    with pytest.raises(IntegrityError) as caught:
        with session.begin_nested():
            action()
            session.flush()
    assert caught.value.orig.sqlstate in states
    assert caught.value.orig.diag.constraint_name in constraints


@pytest.fixture
def seeded(session):
    """Two owners; the first conversation has a completed pair and a retried question."""
    first_user = add_user(session, "a@example.test")
    second_user = add_user(session, "b@example.test")
    first = add_conversation(session, first_user)
    second = add_conversation(session, second_user)
    greeting = save(session, question(first, "q-demo-1"))
    todo = save(session, question(first, "q-demo-2", content="오늘 할 일을 정리해 줘."))
    # The same request key is allowed in another conversation.
    other = save(session, question(second, "q-demo-1", content="다른 사용자의 질문이야."))
    greeting_answer = save(session, answer(first, greeting))
    save(session, answer(first, todo, 1, "failed"))
    save(session, answer(first, todo, 2, "interrupted"))
    save(session, answer(first, todo, 3, "pending"))
    return {
        "first_user": first_user,
        "first": first,
        "second": second,
        "greeting": greeting,
        "todo": todo,
        "other": other,
        "greeting_answer": greeting_answer,
    }


def counts(session):
    return (
        session.scalar(select(func.count()).select_from(Conversation)),
        session.scalar(select(func.count()).select_from(Message)),
    )


def test_schema_matches_database_design(orm_test_connection):
    inspector = inspect(orm_test_connection)
    types = {
        table: {column["name"]: column["type"] for column in inspector.get_columns(table)}
        for table in ("users", "conversations", "messages")
    }
    for table, column in [
        ("users", "id"),
        ("conversations", "user_id"),
        ("messages", "conversation_id"),
        ("messages", "reply_to_message_id"),
    ]:
        assert isinstance(types[table][column], BigInteger)
    for table, column in [("conversations", "updated_at"), ("messages", "created_at"), ("messages", "completed_at")]:
        assert types[table][column].timezone is True
    foreign_keys = inspector.get_foreign_keys("conversations") + inspector.get_foreign_keys("messages")
    assert {key["name"] for key in foreign_keys} == {
        "conversations_user_fk",
        "messages_conversation_fk",
        "messages_reply_same_conversation_fk",
    }
    assert all(key["options"].get("ondelete") == "RESTRICT" for key in foreign_keys)
    assert {check["name"] for check in inspector.get_check_constraints("messages")} == {
        "messages_role_ck",
        "messages_status_ck",
        "messages_shape_ck",
    }
    indexes = {index["name"]: index for index in inspector.get_indexes("messages")}
    unique = {name for name, index in indexes.items() if index["unique"]}
    assert unique >= {
        "messages_question_key_uq",
        "messages_attempt_uq",
        "messages_retry_key_uq",
        "messages_one_pending_uq",
    }
    assert {"messages_order_idx", "messages_reply_idx"} <= set(indexes)
    assert "conversations_owner_list_idx" in {index["name"] for index in inspector.get_indexes("conversations")}


def test_valid_rows_are_stored(session, seeded):
    assert counts(session) == (2, 7)
    stored = session.get(Message, seeded["greeting_answer"])
    assert (stored.role, stored.status, stored.attempt_no) == ("assistant", "completed", 1)
    assert stored.reply_to_message_id == seeded["greeting"]
    assert stored.created_at is not None


def test_missing_user_and_conversation_are_rejected(session, seeded):
    assert_rejected(session, lambda: session.add(Conversation(user_id=-1)), FOREIGN_KEY, "conversations_user_fk")
    assert_rejected(session, lambda: session.add(question(-1, "q-x")), FOREIGN_KEY, "messages_conversation_fk")
    assert counts(session) == (2, 7)


def test_reply_to_a_message_in_another_conversation_is_rejected(session, seeded):
    assert_rejected(
        session,
        lambda: session.add(answer(seeded["second"], seeded["greeting"], 2)),
        FOREIGN_KEY,
        "messages_reply_same_conversation_fk",
    )


def test_duplicate_keys_attempts_and_pending_answers_are_rejected(session, seeded):
    first, greeting, todo = seeded["first"], seeded["greeting"], seeded["todo"]
    cases = [
        (question(first, "q-demo-1"), "messages_question_key_uq"),
        (answer(first, greeting, 1), "messages_attempt_uq"),
        (answer(first, todo, 9, "failed", retry_key="r-demo-2"), "messages_retry_key_uq"),
        (answer(first, greeting, 2, "pending"), "messages_one_pending_uq"),
    ]
    for row, constraint in cases:
        assert_rejected(session, lambda row=row: session.add(row), UNIQUE, constraint)
    assert counts(session) == (2, 7)


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "system"},
        {"status": "done"},
        {"status": "pending", "completed_at": None},
        {"content": "   "},
        {"content": None},
        {"request_key": None},
        {"request_key": ""},
        {"completed_at": None},
        {"error_code": "provider_failed"},
    ],
)
def test_invalid_questions_are_rejected(session, seeded, overrides):
    row = question(seeded["first"], **{"request_key": "q-x", **overrides})
    assert_rejected(
        session, lambda: session.add(row), CHECK, "messages_role_ck", "messages_status_ck", "messages_shape_ck"
    )


@pytest.mark.parametrize(
    ("attempt_no", "status", "overrides"),
    [
        (2, "completed", {"content": None}),
        (2, "completed", {"content": ""}),
        (2, "completed", {"completed_at": None}),
        (2, "completed", {"error_code": "provider_failed"}),
        (2, "failed", {"content": "반쯤 쓴 답"}),
        (2, "failed", {"completed_at": NOW}),
        (2, "failed", {"error_code": None}),
        (2, "interrupted", {"content": "반쯤 쓴 답"}),
        (2, "interrupted", {"error_code": None}),
        (2, "failed", {"retry_key": None}),
        (1, "failed", {"retry_key": "r-x"}),
        (0, "failed", {}),
        (2, "failed", {"request_key": "q-x"}),
        (2, "failed", {"reply_to_message_id": None}),
    ],
)
def test_invalid_answers_are_rejected(session, seeded, attempt_no, status, overrides):
    row = answer(seeded["first"], seeded["greeting"], attempt_no, status, **overrides)
    assert_rejected(session, lambda: session.add(row), CHECK, "messages_shape_ck")


@pytest.mark.parametrize(
    "overrides", [{"content": "미리 쓴 답"}, {"completed_at": NOW}, {"error_code": "provider_failed"}]
)
def test_invalid_pending_answers_are_rejected(session, seeded, overrides):
    row = answer(seeded["second"], seeded["other"], 1, "pending", **overrides)
    assert_rejected(session, lambda: session.add(row), CHECK, "messages_shape_ck")


def test_referenced_user_conversation_and_question_cannot_be_deleted(session, seeded):
    users = users_table()
    cases = [
        (delete(users).where(users.c.id == seeded["first_user"]), "conversations_user_fk"),
        (delete(Conversation).where(Conversation.id == seeded["first"]), "messages_conversation_fk"),
        (delete(Message).where(Message.id == seeded["greeting"]), "messages_reply_same_conversation_fk"),
    ]
    for statement, constraint in cases:
        assert_rejected(session, lambda statement=statement: session.execute(statement), FOREIGN_KEY, constraint)
    assert counts(session) == (2, 7)


def test_database_alone_does_not_require_a_reply_target_to_be_a_question(session, seeded):
    """Services must check role=user and ownership; the composite key only pins the conversation."""
    row = answer(seeded["first"], seeded["greeting_answer"], 1, "failed")
    assert save(session, row) is not None


def test_repeated_initialization_keeps_existing_rows(session, seeded, orm_test_connection):
    before = counts(session)
    initialize_with_engine(orm_test_connection)
    initialize_with_engine(orm_test_connection)
    assert counts(session) == before == (2, 7)
