"""Registration HTTP integration with guarded PostgreSQL transactions."""

from unittest.mock import patch
from uuid import uuid4
import unicodedata

from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.api.auth import registration_session
from app.main import create_app
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.security.passwords import verify_password
from app.security.session_values import create_csrf_token, create_refresh_digest

pytestmark = pytest.mark.db
PATH = "/api/auth/register"
HEADERS = {"Origin": "http://127.0.0.1:5173"}


@pytest.fixture
def signup_body():
    return {"email": f"api-{uuid4().hex}@example.test", "password": "fixture-only-password"}


@pytest.fixture
def db_client(orm_test_session, auth_settings):
    application = create_app(auth_settings)
    application.dependency_overrides[registration_session] = lambda: orm_test_session
    with TestClient(application, base_url="http://127.0.0.1:8000") as client:
        yield client


def test_http_signup_persists_hash_digest_and_issues_linked_credentials(
    db_client, orm_test_session, auth_settings, signup_body
):
    response = db_client.post(PATH, json=signup_body, headers=HEADERS)
    assert response.status_code == 201
    assert not orm_test_session.in_transaction()
    body = response.json()
    user = orm_test_session.get(User, int(body["user"]["id"]))
    assert verify_password(signup_body["password"], user.password_hash)
    assert user.email == signup_body["email"]
    raw_refresh = response.cookies["refresh_token"]
    claims = jwt.decode(
        raw_refresh,
        auth_settings.refresh_key,
        algorithms=["HS256"],
        audience=auth_settings.audience,
        issuer=auth_settings.issuer,
    )
    assert claims["sub"] == body["user"]["id"]
    assert body["csrf_token"] == create_csrf_token(claims["jti"], auth_settings)
    assert response.cookies["csrf_token"] == body["csrf_token"]
    session = orm_test_session.scalar(select(RefreshSession).where(RefreshSession.user_id == user.id))
    assert session.token_digest == create_refresh_digest(raw_refresh, auth_settings)
    assert raw_refresh not in response.text and user.password_hash not in response.text


def test_http_duplicate_does_not_replace_existing_cookies_or_create_session(db_client, orm_test_session, signup_body):
    first = db_client.post(PATH, json=signup_body, headers=HEADERS)
    sessions_before = orm_test_session.scalar(select(func.count()).select_from(RefreshSession))
    duplicate = {**signup_body, "email": f" {signup_body['email'].upper()} "}
    response = db_client.post(PATH, json=duplicate, headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "local_email_exists"
    assert "set-cookie" not in response.headers
    assert db_client.cookies["refresh_token"] == first.cookies["refresh_token"]
    assert orm_test_session.scalar(select(func.count()).select_from(RefreshSession)) == sessions_before


def test_http_origin_failure_creates_no_user(db_client, orm_test_session, signup_body):
    response = db_client.post(PATH, json=signup_body)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert orm_test_session.scalar(select(User.id).where(User.email == signup_body["email"])) is None


def test_http_commit_failure_rolls_back_both_rows_and_emits_no_credentials(db_client, orm_test_session, signup_body):
    sessions_before = orm_test_session.scalar(select(func.count()).select_from(RefreshSession))

    def fail_commit():
        orm_test_session.flush()
        raise OperationalError("private SQL", None, RuntimeError("private details"))

    with patch.object(orm_test_session, "commit", side_effect=fail_commit):
        response = db_client.post(PATH, json=signup_body, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "temporarily_unavailable"
    assert "set-cookie" not in response.headers
    assert "private" not in response.text and "access_token" not in response.text
    assert orm_test_session.scalar(select(User.id).where(User.email == signup_body["email"])) is None
    assert orm_test_session.scalar(select(func.count()).select_from(RefreshSession)) == sessions_before


@pytest.mark.parametrize("password", ["a" * 12, "🔑" * 128, "  e\u0301한글🔑 password  "])
def test_http_signup_preserves_password_boundaries_and_unicode(db_client, orm_test_session, signup_body, password):
    response = db_client.post(PATH, json={**signup_body, "password": password}, headers=HEADERS)
    assert response.status_code == 201
    user = orm_test_session.get(User, int(response.json()["user"]["id"]))
    assert verify_password(password, user.password_hash)
    assert user.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    if password != password.strip():
        assert not verify_password(password.strip(), user.password_hash)
        assert not verify_password(unicodedata.normalize("NFC", password), user.password_hash)
    assert password not in response.text and user.password_hash not in response.text
