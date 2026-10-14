"""Exercise login-issued Bearer tokens and ownership in guarded PostgreSQL."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.main import create_app
from app.models.conversation import Conversation, Message
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.security.tokens import issue_access_token
from app.services.registration import register_local_user

pytestmark = pytest.mark.db
ORIGIN = {"Origin": "http://127.0.0.1:5173"}


@pytest.fixture
def bearer_database(committed_signup_sessions, auth_settings):
    factory = committed_signup_sessions
    User.metadata.create_all(factory.kw["bind"], tables=[Conversation.__table__, Message.__table__])
    bodies = [{"email": f"bearer-{uuid4().hex}@example.test", "password": "fixture-only-password"} for _ in range(2)]
    ids, conversations = [], []
    with factory() as session:
        for body in bodies:
            registered = register_local_user(RegisterRequest.model_validate(body), session, auth_settings)
            user_id = int(registered.user.id)
            row = Conversation(user_id=user_id)
            session.add(row)
            session.commit()
            ids.append(user_id)
            conversations.append(row.id)
    with (
        patch("app.db._session_factory", factory),
        TestClient(create_app(auth_settings), base_url="http://127.0.0.1:8000", client=("192.0.2.1", 123)) as client,
    ):
        tokens = []
        for body in bodies:
            response = client.post("/api/auth/login", json=body, headers=ORIGIN)
            assert response.status_code == 200
            tokens.append(response.json()["access_token"])
        yield client, factory, bodies, ids, conversations, tokens


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_login_token_reads_me_and_creates_conversation_for_its_subject(bearer_database):
    client, factory, bodies, ids, _, tokens = bearer_database
    response = client.get("/api/me", headers=bearer(tokens[0]))
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(ids[0])
    assert response.json()["user"]["email"] == bodies[0]["email"]
    assert set(response.json()["user"]) == {"id", "email", "created_at"}
    created = client.post("/api/conversations", json={}, headers=bearer(tokens[0]))
    assert created.status_code == 201
    with factory() as reader:
        assert reader.get(Conversation, int(created.json()["conversation"]["id"])).user_id == ids[0]


def test_real_bearer_keeps_users_records_separate(bearer_database):
    client, _, _, _, conversations, tokens = bearer_database
    for index, token in enumerate(tokens):
        headers = bearer(token)
        listing = client.get("/api/conversations", headers=headers)
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == [str(conversations[index])]
        own = client.get(f"/api/conversations/{conversations[index]}/messages", headers=headers)
        assert own.status_code == 200 and own.json() == {"items": [], "next_cursor": None}
        other = client.get(f"/api/conversations/{conversations[1 - index]}/messages", headers=headers)
        missing = client.get("/api/conversations/9223372036854775807/messages", headers=headers)
        assert other.status_code == missing.status_code == 404
        assert {key: value for key, value in other.json()["error"].items() if key != "trace_id"} == {
            key: value for key, value in missing.json()["error"].items() if key != "trace_id"
        }


def test_body_and_cookies_cannot_replace_bearer_identity(bearer_database):
    client, factory, _, ids, _, tokens = bearer_database
    headers = {
        **bearer(tokens[0]),
        "Cookie": f"access_token={tokens[1]}; refresh_token={client.cookies['refresh_token']}",
    }
    assert client.get("/api/me", headers=headers).json()["user"]["id"] == str(ids[0])
    response = client.post("/api/conversations", json={"user_id": str(ids[1])}, headers=headers)
    assert response.status_code == 422
    with factory() as reader:
        assert reader.scalar(select(func.count()).select_from(Conversation)) == 2


@pytest.mark.parametrize("kind", ["expired", "refresh", "forged"])
def test_invalid_access_cannot_read_me_or_conversations(bearer_database, auth_settings, kind: str):
    client, _, _, ids, _, tokens = bearer_database
    if kind == "expired":
        token = issue_access_token(ids[0], auth_settings, now=datetime.now(timezone.utc) - timedelta(seconds=900)).token
    elif kind == "refresh":
        token = client.cookies["refresh_token"]
    else:
        header, payload, signature = tokens[0].split(".")
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        token = f"{header}.{payload}.{signature}"
    for path in ("/api/me", "/api/conversations"):
        response = client.get(path, headers=bearer(token))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert response.headers["www-authenticate"] == "Bearer"


def test_me_accepts_public_google_account_with_valid_server_access(bearer_database, auth_settings):
    client, factory, _, _, _, _ = bearer_database
    with factory() as session:
        google = User(email=f"google-{uuid4().hex}@example.test", password_hash=None)
        session.add(google)
        session.commit()
        user_id = google.id
    token = issue_access_token(user_id, auth_settings).token
    response = client.get("/api/me", headers=bearer(token))
    assert response.status_code == 200 and response.json()["user"]["id"] == str(user_id)
    assert set(response.json()["user"]) == {"id", "email", "created_at"}
