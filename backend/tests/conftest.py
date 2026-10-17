"""Reusable authentication data and guarded ORM integration fixtures."""

from copy import deepcopy
import base64
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from sqlalchemy import Integer, String, create_engine, func, inspect, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db import Base, create_db_engine, psycopg_url
from app.db_init import initialize_with_engine
from app.models import register_models
from app.models.auth_identity import AuthIdentity
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.security.settings import AuthSettings, GoogleSettings
from auth_helpers import load_auth_cases
from db_guard import guarded_live_url


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live", action="store_true", help="실제 Codyssey GPT를 호출하는 격리 스모크 시험도 실행합니다."
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """A real provider call never runs by default, in CI or by `-m db`."""
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="실제 GPT 호출은 --run-live로만 실행합니다.")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def auth_environment() -> dict[str, str]:
    """테스트마다 독립적으로 사용하는 가짜 인증 키와 127.0.0.1 설정."""
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
def google_environment(auth_environment: dict[str, str]) -> dict[str, str]:
    """가짜 Google 자격 증명. Google에 접속하거나 개발자의 .env를 읽지 않는다."""
    return {
        **auth_environment,
        "GOOGLE_CLIENT_ID": "123456-fixture.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "fixture-only-google-secret",
        "GOOGLE_REDIRECT_URI": "http://127.0.0.1:8000/api/auth/google/callback",
        "GOOGLE_TRANSACTION_KEY": base64.b64encode(bytes([5]) * 32).decode(),
    }


@pytest.fixture
def google_settings(google_environment: dict[str, str]) -> GoogleSettings:
    return GoogleSettings.from_env(google_environment)


@pytest.fixture(scope="session")
def google_rsa_private_key():
    """실제 서명 검증 테스트에서만 사용하는 일회성 RSA 키. 파일로 저장하지 않는다."""
    return generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def auth_cases() -> dict[str, Any]:
    """각 테스트에 독립적인 인증 입력과 기대 결과를 제공한다."""
    return load_auth_cases()


@pytest.fixture
def auth_user_a(auth_cases: dict[str, Any]) -> dict[str, Any]:
    """사용자 101의 입력, 공개 응답과 서버가 신원을 확인했다고 가정한 테스트용 사용자 정보."""
    return deepcopy(auth_cases["users"]["a"])


@pytest.fixture
def auth_user_b(auth_cases: dict[str, Any]) -> dict[str, Any]:
    """사용자 202의 입력, 공개 응답과 서버가 신원을 확인했다고 가정한 테스트용 사용자 정보."""
    return deepcopy(auth_cases["users"]["b"])


@pytest.fixture(params=["a", "b"], ids=["user-101", "user-202"])
def auth_user(request: pytest.FixtureRequest, auth_cases: dict[str, Any]) -> dict[str, Any]:
    """테스트 코드를 복제하지 않고 사용자별로 한 번씩 실행한다."""
    return deepcopy(auth_cases["users"][request.param])


@pytest.fixture
def unauthenticated_case(auth_cases: dict[str, Any]) -> dict[str, Any]:
    """확인된 사용자 정보와 자격 증명이 없는 요청 및 기대하는 401 응답 예시."""
    return deepcopy(auth_cases["unauthenticated"])


@pytest.fixture(params=[0, 1], ids=["a-body-b", "b-body-a"])
def principal_body_mismatch_case(request: pytest.FixtureRequest, auth_cases: dict[str, Any]) -> dict[str, Any]:
    """요청 본문의 사용자 ID를 다른 사용자로 바꾼 두 사례를 각각 실행한다."""
    return deepcopy(auth_cases["principal_body_mismatch_cases"][request.param])


class OrmProbe(Base):
    __tablename__ = "b71_orm_test_probe"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(50))


@pytest.fixture
def orm_test_connection():
    """DDL, 저장점과 데이터를 모두 롤백하는 검증된 테스트 DB 연결."""
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
    """외부 트랜잭션을 확정하지 않고 모델 테스트 안에서 커밋할 수 있게 한다."""
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:
        yield session


@pytest.fixture
def committed_signup_sessions():
    """임시 스키마에서만 실제 가입 커밋을 허용한다."""
    engine = create_db_engine(guarded_live_url())
    schema = f"signup_test_{uuid4().hex}"
    scoped = engine.execution_options(schema_translate_map={None: schema})
    created = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        User.metadata.create_all(scoped, tables=[User.__table__, RefreshSession.__table__])
        yield sessionmaker(bind=scoped, autoflush=False, expire_on_commit=False)
    finally:
        try:
            if created:
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            engine.dispose()


@pytest.fixture
def committed_google_sessions():
    """Test real concurrent Google account commits in a temporary schema of the guarded test DB."""
    engine = create_db_engine(guarded_live_url())
    schema = f"google_test_{uuid4().hex}"
    scoped = engine.execution_options(schema_translate_map={None: schema})
    created = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        User.metadata.create_all(scoped, tables=[User.__table__, AuthIdentity.__table__, RefreshSession.__table__])
        yield sessionmaker(bind=scoped, autoflush=False, expire_on_commit=False)
    finally:
        try:
            if created:
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            engine.dispose()


pytest_plugins = ["google_callback_helpers"]
