"""Real register/login JWTs protect mock chat persisted in a guarded temporary schema.

Only the DB session factory is bound to the existing isolated-schema fixture.
Authentication, principal verification and the public MockProvider are unchanged.
The app's chat job worker runs, so a queued question is answered as in service (#53).
"""

import time
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.main import create_app
from app.models.chat_job import ChatJob, ChatQueueMember, ConversationHold
from app.models.conversation import Conversation, Message
from app.models.quota import ChatQuota, QuotaReservation

pytestmark = pytest.mark.db
ORIGIN = {"Origin": "http://127.0.0.1:5173"}
BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture
def chat_database(committed_signup_sessions, auth_settings, monkeypatch):
    factory = committed_signup_sessions
    models = (Conversation, Message, ChatQuota, QuotaReservation, ChatQueueMember, ChatJob, ConversationHold)
    Conversation.metadata.create_all(factory.kw["bind"], tables=[model.__table__ for model in models])
    monkeypatch.setenv("CHAT_WORKER", "on")
    app = create_app(auth_settings)
    with patch("app.db._session_factory", factory):
        yield app, factory


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def register_and_login(client):
    body = {"email": f"chat-{uuid4().hex}@example.test", "password": "fixture-only-password"}
    registered = client.post("/api/auth/register", json=body, headers=ORIGIN)
    assert registered.status_code == 201
    logged_in = client.post("/api/auth/login", json=body, headers=ORIGIN)
    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["id"] == registered.json()["user"]["id"]
    return body, logged_in.json()


def create_conversation(client, access):
    response = client.post("/api/conversations", json={}, headers=bearer(access))
    assert response.status_code == 201
    return response.json()["conversation"]["id"]


def send_mock_question(client, access, conversation_id):
    """Queue a question, then read its job until the worker has stored the answer."""
    body = {"content": "가짜 교통법 질문입니다.", "request_key": "auth-chat-1"}
    response = client.post(f"/api/conversations/{conversation_id}/messages", json=body, headers=bearer(access))
    assert response.status_code == 202 and response.json()["answer"] is None
    job_id = response.json()["job"]["id"]
    deadline = time.monotonic() + 15
    while True:
        found = client.get(f"/api/chat/jobs/{job_id}", headers=bearer(access))
        assert found.status_code == 200
        if found.json()["job"]["state"] == "completed" or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert found.json()["job"]["state"] == "completed"
    question, answer = found.json()["question"], found.json()["answer"]
    assert (question["role"], question["status"], question["content"], question["request_key"]) == (
        "user",
        "completed",
        body["content"],
        body["request_key"],
    )
    assert (answer["role"], answer["status"], answer["content"]) == ("assistant", "completed", "모의 답변입니다.")
    assert answer["reply_to_message_id"] == question["id"]
    assert answer["completed_at"] is not None and answer["error_code"] is None
    return [question, answer]


def assert_history(client, access, conversation_id, expected):
    response = client.get(f"/api/conversations/{conversation_id}/messages", headers=bearer(access))
    assert response.status_code == 200
    assert response.json() == {"items": expected, "next_cursor": None}


def test_login_chat_persists_across_fresh_client_and_refresh(chat_database):
    app, factory = chat_database
    with TestClient(app, base_url=BASE_URL, client=("192.0.2.1", 123)) as first:
        body, login = register_and_login(first)
        conversation_id = create_conversation(first, login["access_token"])
        pair = send_mock_question(first, login["access_token"], conversation_id)
        listing = first.get("/api/conversations", headers=bearer(login["access_token"]))
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == [conversation_id]

    # A different DB session sees committed rows, independent of either HTTP client.
    with factory() as reader:
        conversation = reader.get(Conversation, int(conversation_id))
        assert conversation is not None and str(conversation.user_id) == login["user"]["id"]
        rows = reader.scalars(
            select(Message).where(Message.conversation_id == int(conversation_id)).order_by(Message.id)
        )
        assert [(str(row.id), row.role, row.status, row.content) for row in rows] == [
            (message["id"], message["role"], message["status"], message["content"]) for message in pair
        ]

    with TestClient(app, base_url=BASE_URL, client=("192.0.2.1", 123)) as fresh:
        assert not fresh.cookies
        relogin = fresh.post("/api/auth/login", json=body, headers=ORIGIN)
        assert relogin.status_code == 200 and relogin.json()["user"]["id"] == login["user"]["id"]
        assert_history(fresh, relogin.json()["access_token"], conversation_id, pair)
        csrf = fresh.get("/api/auth/csrf")
        assert csrf.status_code == 200
        refreshed = fresh.post("/api/auth/refresh", headers={**ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]})
        assert refreshed.status_code == 200
        new_access = refreshed.json()["access_token"]
        assert_history(fresh, new_access, conversation_id, pair)
        logout = fresh.post("/api/auth/logout", headers={**ORIGIN, "X-CSRF-Token": refreshed.json()["csrf_token"]})
        assert logout.status_code == 204
        assert fresh.get("/api/auth/csrf").status_code == 401
        # Logout revokes this refresh lineage; issued access lasts until its own expiry.
        assert_history(fresh, new_access, conversation_id, pair)

    with factory() as reader:
        assert reader.scalar(select(func.count()).select_from(Message)) == 2


def test_two_real_users_cannot_read_or_append_each_others_chat(chat_database):
    app, factory = chat_database
    with (
        TestClient(app, base_url=BASE_URL, client=("192.0.2.1", 123)) as first,
        TestClient(app, base_url=BASE_URL, client=("192.0.2.1", 123)) as second,
    ):
        _, login_a = register_and_login(first)
        _, login_b = register_and_login(second)
        assert login_a["user"]["id"] != login_b["user"]["id"]
        access_a, access_b = login_a["access_token"], login_b["access_token"]
        mine_a = create_conversation(first, access_a)
        mine_b = create_conversation(second, access_b)
        pair_a = send_mock_question(first, access_a, mine_a)
        pair_b = send_mock_question(second, access_b, mine_b)
        for client, access, mine, foreign in ((first, access_a, mine_a, mine_b), (second, access_b, mine_b, mine_a)):
            listing = client.get("/api/conversations", headers=bearer(access))
            assert listing.status_code == 200
            assert [item["id"] for item in listing.json()["items"]] == [mine]
            path = f"/api/conversations/{foreign}/messages"
            denied_read = client.get(path, headers=bearer(access))
            denied_write = client.post(
                path, json={"content": "타인 대화 공격", "request_key": "foreign"}, headers=bearer(access)
            )
            for response in (denied_read, denied_write):
                assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
        assert_history(first, access_a, mine_a, pair_a)
        assert_history(second, access_b, mine_b, pair_b)

    with factory() as reader:
        assert reader.scalar(select(func.count()).select_from(Conversation)) == 2
        assert reader.scalar(select(func.count()).select_from(Message)) == 4
        assert reader.scalar(select(func.count()).select_from(Message).where(Message.request_key == "foreign")) == 0


@pytest.mark.parametrize("credential", ["missing", "tampered-signature"])
def test_cookies_without_valid_access_cannot_read_or_mutate_chat(chat_database, credential):
    app, factory = chat_database
    with TestClient(app, base_url=BASE_URL, client=("192.0.2.1", 123)) as client:
        _, login = register_and_login(client)
        conversation_id = create_conversation(client, login["access_token"])
        pair = send_mock_question(client, login["access_token"], conversation_id)
        assert client.cookies.get("refresh_token")
        headers = {}
        if credential == "tampered-signature":
            # Corrupt an HTTP-issued token, rather than manufacturing an authenticated one.
            unsigned, signature = login["access_token"].rsplit(".", 1)
            replacement = "A" if signature[0] != "A" else "B"
            headers = bearer(unsigned + "." + replacement + signature[1:])
        path = f"/api/conversations/{conversation_id}/messages"
        responses = [
            client.get("/api/conversations", headers=headers),
            client.post("/api/conversations", json={}, headers=headers),
            client.get(path, headers=headers),
            client.post(path, json={"content": "거부할 질문", "request_key": "denied"}, headers=headers),
        ]
        for response in responses:
            assert response.status_code == 401 and response.json()["error"]["code"] == "unauthenticated"
        assert_history(client, login["access_token"], conversation_id, pair)
    with factory() as reader:
        assert reader.scalar(select(func.count()).select_from(Conversation)) == 1
        assert reader.scalar(select(func.count()).select_from(Message)) == 2
