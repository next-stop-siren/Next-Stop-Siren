"""Authentication schema and constraint checks against guarded PostgreSQL."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema

from app.db_init import initialize_with_engine
from app.models.auth_identity import AuthIdentity
from app.models.refresh_session import RefreshSession
from app.models.user import User

pytestmark = pytest.mark.db
NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


@pytest.fixture
def users(orm_test_session, auth_user_a, auth_user_b):
    prefix = uuid4().hex
    result = [
        User(email=f"{prefix}-{case['register_input']['email']}", password_hash="fixture-only-hash")
        for case in (auth_user_a, auth_user_b)
    ]
    orm_test_session.add_all(result)
    orm_test_session.flush()
    return result


def identity(user_id, **values):
    return AuthIdentity(user_id=user_id, issuer="https://accounts.google.com", subject=uuid4().hex, **values)


def refresh(user_id, **values):
    fields = {"token_digest": uuid4().bytes * 2, "issued_at": NOW, "expires_at": NOW + timedelta(days=14)}
    fields.update(values)
    return RefreshSession(user_id=user_id, **fields)


@contextmanager
def rejected(session, sqlstate, constraint=None):
    """Check the intended database failure and keep the session usable."""
    with pytest.raises(IntegrityError) as caught:
        with session.begin_nested():
            yield
            session.flush()
    assert caught.value.orig.sqlstate == sqlstate
    if constraint is not None:
        assert caught.value.orig.diag.constraint_name == constraint


def test_schema_types_identity_defaults_and_constraints(orm_test_connection):
    inspector = inspect(orm_test_connection)
    expected = {
        "users": {"id": "BIGINT", "email": "TEXT", "password_hash": "TEXT", "created_at": "TIMESTAMP"},
        "auth_identities": {
            "id": "BIGINT",
            "user_id": "BIGINT",
            "provider": "TEXT",
            "issuer": "TEXT",
            "subject": "TEXT",
            "created_at": "TIMESTAMP",
        },
        "refresh_sessions": {
            "id": "BIGINT",
            "user_id": "BIGINT",
            "token_digest": "BYTEA",
            "rotated_from_id": "BIGINT",
            "issued_at": "TIMESTAMP",
            "expires_at": "TIMESTAMP",
            "revoked_at": "TIMESTAMP",
        },
    }
    nullable = {("users", "password_hash"), ("refresh_sessions", "rotated_from_id"), ("refresh_sessions", "revoked_at")}
    for table, types in expected.items():
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        assert set(columns) == set(types)
        assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"]
        assert columns["id"]["identity"]["always"] is True
        for name, type_name in types.items():
            assert str(columns[name]["type"]) == type_name
            assert columns[name]["nullable"] == ((table, name) in nullable)
            if type_name == "TIMESTAMP":
                assert columns[name]["type"].timezone is True
            if name in {"created_at", "issued_at"}:
                assert columns[name]["default"] == "now()"
            elif name == "provider":
                assert columns[name]["default"] == "'google'::text"
            elif name != "id":
                assert columns[name]["default"] is None

    for model in (User, AuthIdentity, RefreshSession):
        table = model.__table__
        checks = {item["name"] for item in inspector.get_check_constraints(table.name)}
        unique = {item["name"]: item["column_names"] for item in inspector.get_unique_constraints(table.name)}
        for constraint in table.constraints:
            if constraint.__class__.__name__ == "CheckConstraint":
                assert constraint.name in checks
            elif constraint.__class__.__name__ == "UniqueConstraint":
                assert unique[constraint.name] == list(constraint.columns.keys())

    for table in ("auth_identities", "refresh_sessions"):
        foreign_keys = inspector.get_foreign_keys(table)
        user_fk = next(item for item in foreign_keys if item["constrained_columns"] == ["user_id"])
        assert user_fk["referred_table"] == "users"
        assert user_fk["referred_columns"] == ["id"]
        assert all(item["options"]["ondelete"] == "RESTRICT" for item in foreign_keys)
    predecessor = next(
        item
        for item in inspector.get_foreign_keys("refresh_sessions")
        if item["constrained_columns"] == ["rotated_from_id", "user_id"]
    )
    assert predecessor["referred_table"] == "refresh_sessions"
    assert predecessor["referred_columns"] == ["id", "user_id"]

    local_email = next(item for item in inspector.get_indexes("users") if item["name"] == "users_local_email_uq")
    assert local_email["unique"] is True
    assert local_email["expressions"] == ["lower(email)"]
    assert "password_hash IS NOT NULL" in local_email["dialect_options"]["postgresql_where"]
    google_index = next(
        item for item in inspector.get_indexes("auth_identities") if item["name"] == "auth_identities_user_idx"
    )
    assert google_index["column_names"] == ["user_id"]
    session_index = next(
        item for item in inspector.get_indexes("refresh_sessions") if item["name"] == "refresh_sessions_user_idx"
    )
    assert session_index["column_names"] == ["user_id", "expires_at"]
    assert "desc" in session_index["column_sorting"]["expires_at"]


def test_local_and_google_accounts_with_same_email_are_separate(orm_test_session, users):
    local = users[0]
    google = User(email=local.email)
    another_google = User(email=local.email)
    orm_test_session.add_all([google, another_google])
    orm_test_session.flush()
    google_identity = identity(google.id)
    orm_test_session.add(google_identity)
    orm_test_session.commit()
    orm_test_session.expire_all()
    assert local.id != google.id != another_google.id
    assert google.password_hash is None
    assert google.created_at.tzinfo is not None
    assert google_identity.provider == "google"
    assert google_identity.created_at.tzinfo is not None
    with rejected(orm_test_session, "23505", "users_local_email_uq"):
        orm_test_session.add(User(email=local.email.upper(), password_hash="fixture-only-hash"))


@pytest.mark.parametrize(
    "email,password_hash,constraint",
    [
        (" a@example.test", None, "users_email_check"),
        ("a@example.test ", None, "users_email_check"),
        ("ab", None, "users_email_check"),
        ("a" * 321, None, "users_email_check"),
        ("valid@example.test", "", "users_password_hash_check"),
    ],
)
def test_invalid_user_values_are_rejected(orm_test_session, email, password_hash, constraint):
    with rejected(orm_test_session, "23514", constraint):
        orm_test_session.add(User(email=email, password_hash=password_hash))


@pytest.mark.parametrize("length", [3, 320])
def test_email_length_boundaries(orm_test_session, length):
    user = User(email="a" * length)
    orm_test_session.add(user)
    orm_test_session.flush()
    assert user.id is not None


@pytest.mark.parametrize(
    "column,value,constraint",
    [
        ("provider", "other", "auth_identities_provider_check"),
        ("issuer", "", "auth_identities_issuer_subject_check"),
        ("subject", "", "auth_identities_issuer_subject_check"),
    ],
)
def test_invalid_google_identity_is_rejected(orm_test_session, users, column, value, constraint):
    record = identity(users[0].id)
    setattr(record, column, value)
    with rejected(orm_test_session, "23514", constraint):
        orm_test_session.add(record)


def test_google_identity_pair_is_unique_across_users(orm_test_session, users):
    first = identity(users[0].id)
    orm_test_session.add(first)
    orm_test_session.flush()
    duplicate = identity(users[1].id)
    duplicate.subject = first.subject
    with rejected(orm_test_session, "23505", "auth_identities_issuer_subject_uq"):
        orm_test_session.add(duplicate)
    other_issuer = identity(users[1].id)
    other_issuer.issuer = "https://fixture-issuer.example.test"
    other_issuer.subject = first.subject
    orm_test_session.add_all([other_issuer, identity(users[1].id)])
    orm_test_session.flush()


@pytest.mark.parametrize(
    "factory,constraint",
    [
        (identity, "auth_identities_user_fk"),
        (refresh, "refresh_sessions_user_fk"),
    ],
)
def test_missing_user_is_rejected(orm_test_session, factory, constraint):
    with rejected(orm_test_session, "23503", constraint):
        orm_test_session.add(factory(-1))


def test_refresh_chain_and_independent_logins(orm_test_session, users):
    root = refresh(users[0].id)
    independent = refresh(users[0].id)
    orm_test_session.add_all([root, independent])
    orm_test_session.flush()
    root.revoked_at = NOW + timedelta(minutes=1)
    successor = refresh(users[0].id, rotated_from_id=root.id, issued_at=root.revoked_at)
    orm_test_session.add(successor)
    default_issued = RefreshSession(
        user_id=users[1].id, token_digest=uuid4().bytes * 2, expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    orm_test_session.add(default_issued)
    orm_test_session.commit()
    orm_test_session.expire_all()
    assert successor.rotated_from_id == root.id
    assert successor.expires_at == root.expires_at
    assert root.revoked_at == NOW + timedelta(minutes=1)
    assert independent.rotated_from_id is None
    assert successor.revoked_at is None
    assert isinstance(successor.token_digest, bytes)
    assert default_issued.issued_at.tzinfo is not None


@pytest.mark.parametrize("length", [0, 31, 33])
def test_digest_length_is_rejected(orm_test_session, users, length):
    with rejected(orm_test_session, "23514", "refresh_sessions_digest_length_check"):
        orm_test_session.add(refresh(users[0].id, token_digest=b"x" * length))


def test_digest_duplicate_is_rejected(orm_test_session, users):
    first = refresh(users[0].id)
    orm_test_session.add(first)
    orm_test_session.flush()
    with rejected(orm_test_session, "23505", "refresh_sessions_token_digest_uq"):
        orm_test_session.add(refresh(users[1].id, token_digest=first.token_digest))


@pytest.mark.parametrize("offset", [-1, 0])
def test_expiry_must_be_after_issue(orm_test_session, users, offset):
    with rejected(orm_test_session, "23514", "refresh_sessions_expiry_check"):
        orm_test_session.add(refresh(users[0].id, expires_at=NOW + timedelta(seconds=offset)))


def test_invalid_predecessors_and_second_successor_are_rejected(orm_test_session, users):
    root = refresh(users[0].id)
    orm_test_session.add(root)
    orm_test_session.flush()
    with rejected(orm_test_session, "23503", "refresh_sessions_predecessor_fk"):
        orm_test_session.add(refresh(users[0].id, rotated_from_id=-1))
    with rejected(orm_test_session, "23503", "refresh_sessions_predecessor_fk"):
        orm_test_session.add(refresh(users[1].id, rotated_from_id=root.id))
    orm_test_session.add(refresh(users[0].id, rotated_from_id=root.id))
    orm_test_session.flush()
    with rejected(orm_test_session, "23505", "refresh_sessions_rotated_from_uq"):
        orm_test_session.add(refresh(users[0].id, rotated_from_id=root.id))


@pytest.mark.parametrize(
    "factory,constraint",
    [
        (identity, "auth_identities_user_fk"),
        (refresh, "refresh_sessions_user_fk"),
    ],
)
def test_referenced_user_cannot_be_deleted(orm_test_session, users, factory, constraint):
    orm_test_session.add(factory(users[0].id))
    orm_test_session.flush()
    with rejected(orm_test_session, "23503", constraint):
        orm_test_session.execute(delete(User).where(User.id == users[0].id))


def test_referenced_predecessor_cannot_be_deleted(orm_test_session, users):
    root = refresh(users[0].id)
    orm_test_session.add(root)
    orm_test_session.flush()
    orm_test_session.add(refresh(users[0].id, rotated_from_id=root.id))
    orm_test_session.flush()
    with rejected(orm_test_session, "23503", "refresh_sessions_predecessor_fk"):
        orm_test_session.execute(delete(RefreshSession).where(RefreshSession.id == root.id))


def test_repeated_initialization_preserves_product_rows_and_savepoint_rollback(orm_test_session, users):
    connection = orm_test_session.get_bind()
    record = identity(users[0].id)
    token = refresh(users[0].id)
    orm_test_session.add_all([record, token])
    orm_test_session.commit()
    initialize_with_engine(connection)
    initialize_with_engine(connection)
    with Session(connection, join_transaction_mode="create_savepoint") as other:
        assert other.get(User, users[0].id).email == users[0].email
        assert other.get(AuthIdentity, record.id).subject == record.subject
        assert other.get(RefreshSession, token.id).token_digest == token.token_digest
        rolled_back = refresh(users[0].id)
        other.add(rolled_back)
        other.flush()
        rolled_back_id = rolled_back.id
        other.rollback()
    assert orm_test_session.scalar(select(RefreshSession).where(RefreshSession.id == rolled_back_id)) is None


def test_empty_schema_initialization_preserves_existing_tables(orm_test_connection, auth_user_a):
    """Exercise first creation without deleting or altering an existing schema."""
    engine = orm_test_connection.engine
    schema = f"s02_test_{uuid4().hex}"
    public_before = set(inspect(engine).get_table_names(schema="public"))
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(CreateSchema(schema))
            assert inspect(connection).get_table_names(schema=schema) == []
            translated = connection.execution_options(schema_translate_map={None: schema})
            initialize_with_engine(translated)
            product_tables = {"users", "auth_identities", "refresh_sessions"}
            assert product_tables <= set(inspect(connection).get_table_names(schema=schema))
            with Session(translated, join_transaction_mode="create_savepoint") as session:
                user = User(email=auth_user_a["register_input"]["email"], password_hash="fixture-only-hash")
                session.add(user)
                session.flush()
                record = identity(user.id)
                token = refresh(user.id)
                session.add_all([record, token])
                session.commit()
                initialize_with_engine(translated)
                assert session.get(AuthIdentity, record.id).user_id == user.id
                assert session.get(RefreshSession, token.id).token_digest == token.token_digest
                with rejected(session, "23505", "users_local_email_uq"):
                    session.add(User(email=user.email.upper(), password_hash="fixture-only-hash"))
                with rejected(session, "23514", "refresh_sessions_digest_length_check"):
                    session.add(refresh(user.id, token_digest=b"short"))
        finally:
            transaction.rollback()
    assert schema not in inspect(engine).get_schema_names()
    assert set(inspect(engine).get_table_names(schema="public")) == public_before
