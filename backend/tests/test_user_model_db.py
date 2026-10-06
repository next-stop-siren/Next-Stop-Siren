"""Real PostgreSQL checks for the common user ORM constraints."""

import pytest
from sqlalchemy import BigInteger, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.user import User

pytestmark = pytest.mark.db

UNIQUE = "23505"
CHECK = "23514"


@pytest.fixture
def session(orm_test_connection):
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:
        yield session


def save(session, email, password_hash=None):
    user = User(email=email, password_hash=password_hash)
    session.add(user)
    session.flush()
    return user


def assert_rejected(session, user, state, constraint):
    """The row fails with the expected constraint and leaves earlier rows in place."""
    with pytest.raises(IntegrityError) as caught:
        with session.begin_nested():
            session.add(user)
            session.flush()
    assert caught.value.orig.sqlstate == state
    assert caught.value.orig.diag.constraint_name == constraint


def count(session):
    return session.scalar(select(func.count()).select_from(User))


def test_schema_matches_database_design(orm_test_connection):
    inspector = inspect(orm_test_connection)
    columns = {column["name"]: column for column in inspector.get_columns("users")}
    assert set(columns) == {"id", "email", "password_hash", "created_at"}
    assert isinstance(columns["id"]["type"], BigInteger)
    assert columns["id"]["identity"]["always"] is True
    assert columns["email"]["nullable"] is False
    assert columns["password_hash"]["nullable"] is True
    assert columns["created_at"]["type"].timezone is True
    assert {check["name"] for check in inspector.get_check_constraints("users")} == {
        "users_email_ck",
        "users_password_hash_ck",
    }
    indexes = {index["name"]: index for index in inspector.get_indexes("users")}
    assert indexes["users_local_email_uq"]["unique"]


def test_local_and_google_accounts_with_the_same_email_are_separate_rows(session):
    local = save(session, "a@example.test", "fake-hash")
    google = save(session, "a@example.test")
    another_google = save(session, "a@example.test")
    assert len({local.id, google.id, another_google.id}) == 3
    assert local.created_at is not None


def test_duplicate_local_email_is_rejected_regardless_of_case(session):
    save(session, "a@example.test", "fake-hash")
    assert_rejected(
        session, User(email="A@Example.Test", password_hash="other-fake-hash"), UNIQUE, "users_local_email_uq"
    )
    assert count(session) == 1


@pytest.mark.parametrize("email", [" a@example.test", "a@example.test ", "ab", "a" * 321])
def test_invalid_email_is_rejected(session, email):
    assert_rejected(session, User(email=email), CHECK, "users_email_ck")


def test_empty_password_hash_is_rejected(session):
    assert_rejected(session, User(email="a@example.test", password_hash=""), CHECK, "users_password_hash_ck")
