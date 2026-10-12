"""Reusable authentication data and guarded ORM integration fixtures."""

from copy import deepcopy
import base64
from typing import Any

import pytest
from sqlalchemy import Integer, String, create_engine, func, inspect, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base, psycopg_url
from app.db_init import initialize_with_engine
from app.models import register_models
from app.security.settings import AuthSettings
from auth_helpers import load_auth_cases
from db_guard import guarded_live_url


@pytest.fixture
def auth_environment() -> dict[str, str]:
    """Independent, fake authentication keys and 127.0.0.1 settings."""
    names = ("AUTH_ACCESS_KEY", "AUTH_REFRESH_KEY", "AUTH_REFRESH_DIGEST_KEY", "AUTH_CSRF_KEY")
    return {
        **{name: base64.b64encode(bytes([index]) * 32).decode() for index, name in enumerate(names, start=1)},
        "AUTH_ENV": "development",
        "AUTH_ACCESS_KID": "test-access-v1",
        "AUTH_REFRESH_KID": "test-refresh-v1",
        "AUTH_JWT_ISSUER": "fixture-issuer",
        "AUTH_JWT_AUDIENCE": "fixture-api",
        "AUTH_API_ORIGIN": "http://127.0.0.1:8000",
        "AUTH_ALLOWED_ORIGINS": "http://127.0.0.1:5173",
        "AUTH_COOKIE_SECURE": "false",
    }


@pytest.fixture
def auth_settings(auth_environment: dict[str, str]) -> AuthSettings:
    return AuthSettings.from_env(auth_environment)


@pytest.fixture
def auth_cases() -> dict[str, Any]:
    """Provide fresh authentication inputs and expectations for each test."""
    return load_auth_cases()


@pytest.fixture
def auth_user_a(auth_cases: dict[str, Any]) -> dict[str, Any]:
    """User 101: inputs, public responses and a mock verified principal."""
    return deepcopy(auth_cases["users"]["a"])


@pytest.fixture
def auth_user_b(auth_cases: dict[str, Any]) -> dict[str, Any]:
    """User 202: inputs, public responses and a mock verified principal."""
    return deepcopy(auth_cases["users"]["b"])


@pytest.fixture(params=["a", "b"], ids=["user-101", "user-202"])
def auth_user(request: pytest.FixtureRequest, auth_cases: dict[str, Any]) -> dict[str, Any]:
    """Run a consuming test once per user without manually duplicating it."""
    return deepcopy(auth_cases["users"][request.param])


@pytest.fixture
def unauthenticated_case(auth_cases: dict[str, Any]) -> dict[str, Any]:
    """No principal or credentials, with the expected 401 response example."""
    return deepcopy(auth_cases["unauthenticated"])


@pytest.fixture(params=[0, 1], ids=["a-body-b", "b-body-a"])
def principal_body_mismatch_case(request: pytest.FixtureRequest, auth_cases: dict[str, Any]) -> dict[str, Any]:
    """Run both attempts to substitute a different user ID in a request body."""
    return deepcopy(auth_cases["principal_body_mismatch_cases"][request.param])


class OrmProbe(Base):
    __tablename__ = "b71_orm_test_probe"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(50))


@pytest.fixture
def orm_test_connection():
    """A verified test connection whose DDL, savepoints and data all roll back."""
    url = guarded_live_url()
    engine = create_engine(psycopg_url(url), connect_args={"connect_timeout": 3})
    register_models()
    before = set(inspect(engine).get_table_names())
    managed = set(Base.metadata.tables)
    with engine.connect() as reader:
        counts_before = {
            name: reader.scalar(select(func.count()).select_from(Base.metadata.tables[name]))
            for name in before & managed
        }
    connection = engine.connect()
    transaction = connection.begin()
    try:
        initialize_with_engine(connection)
        yield connection
    finally:
        transaction.rollback()
        connection.close()
        try:
            after = set(inspect(engine).get_table_names())
            assert after & managed == before & managed
            with engine.connect() as reader:
                for name, count in counts_before.items():
                    assert reader.scalar(select(func.count()).select_from(Base.metadata.tables[name])) == count
        finally:
            engine.dispose()


@pytest.fixture
def orm_test_session(orm_test_connection):
    """Allow model tests to commit without committing the outer transaction."""
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:
        yield session
