"""HTTP behavior of the conversation API with an in-memory stand-in for storage.

Every user, ID, body and time is fake. The stand-in applies the owner limit and the
ordering that the repository contract requires; real queries are checked with #8.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.api.auth import current_user_id
from app.db import get_session
from app.main import app
from app.repositories import conversations as repository

# Larger than JavaScript's largest safe integer, so it must survive as a string.
BIG_ID = 9007199254740993
KST = timezone(timedelta(hours=9))


def at(minute: int) -> datetime:
    return datetime(2026, 1, 2, 3, minute, tzinfo=timezone.utc)


def conversation(conversation_id: int, user_id: int, created: datetime, updated: datetime) -> SimpleNamespace:
    return SimpleNamespace(id=conversation_id, user_id=user_id, created_at=created, updated_at=updated)


def message(message_id: int, role: str, status: str, created: datetime, **fields) -> SimpleNamespace:
    values = {
        "content": None,
        "request_key": None,
        "reply_to_message_id": None,
        "attempt_no": None,
        "retry_key": None,
        "error_code": None,
        "updated_at": created,
        "completed_at": None,
    }
    return SimpleNamespace(id=message_id, role=role, status=status, created_at=created, **{**values, **fields})


class FakeStore:
    def __init__(self) -> None:
        self.commits = 0
        self.calls = 0
        self.conversations = [
            conversation(501, 101, at(5), at(7)),
            conversation(502, 202, at(20), at(21)),
            conversation(503, 101, at(30), at(30)),
            conversation(BIG_ID, 101, at(40), at(40)),
        ]
        self.messages = {
            501: [
                message(9001, "user", "completed", at(6), content="안녕?", request_key="q-demo-1", completed_at=at(6)),
                message(
                    9002,
                    "assistant",
                    "completed",
                    at(6),
                    content="안녕하세요.",
                    reply_to_message_id=9001,
                    attempt_no=1,
                    updated_at=at(7),
                    completed_at=at(7),
                ),
                message(
                    9003, "user", "completed", at(10), content="정리해 줘.", request_key="q-demo-2", completed_at=at(10)
                ),
                message(
                    9004,
                    "assistant",
                    "failed",
                    at(10),
                    reply_to_message_id=9003,
                    attempt_no=2,
                    retry_key="r-demo-1",
                    error_code="provider_failed",
                ),
            ],
            502: [
                message(
                    9101,
                    "user",
                    "completed",
                    at(20),
                    content="다른 사용자",
                    request_key="q-demo-1",
                    completed_at=at(20),
                )
            ],
            503: [],
            BIG_ID: [],
        }

    def commit(self) -> None:
        self.commits += 1

    def create_conversation(self, _session, user_id):
        self.calls += 1
        # A non-UTC offset, as a database session in another time zone would return.
        created = datetime(2026, 1, 2, 12, 50, tzinfo=KST)
        row = conversation(601, user_id, created, created)
        self.conversations.append(row)
        return row

    def list_conversations(self, _session, user_id, *, limit, before_id):
        self.calls += 1
        rows = [row for row in self.conversations if row.user_id == user_id]
        rows = [row for row in rows if before_id is None or row.id < before_id]
        return sorted(rows, key=lambda row: row.id, reverse=True)[:limit]

    def list_messages(self, _session, user_id, conversation_id, *, limit, after_id):
        self.calls += 1
        if not any(row.id == conversation_id and row.user_id == user_id for row in self.conversations):
            return None
        rows = [row for row in self.messages[conversation_id] if after_id is None or row.id > after_id]
        return rows[:limit]


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    for name in ("create_conversation", "list_conversations", "list_messages"):
        monkeypatch.setattr(repository, name, getattr(fake, name))
    app.dependency_overrides[get_session] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


def sign_in(user_id: int) -> None:
    """Stand in for access token verification with an already verified user ID."""
    app.dependency_overrides[current_user_id] = lambda: user_id


def call(method: str, path: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def error_of(response: httpx.Response) -> dict:
    """The error body without its per-response trace ID."""
    error = response.json()["error"]
    assert set(error) == {"code", "message", "fields", "trace_id"}
    assert isinstance(error.pop("trace_id"), str)
    return error


def ids(response: httpx.Response) -> list[str]:
    return [item["id"] for item in response.json()["items"]]


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("POST", "/api/conversations", {"json": {}}),
        ("POST", "/api/conversations", {"json": {"user_id": "101"}}),
        ("GET", "/api/conversations", {}),
        ("GET", "/api/conversations?limit=0", {}),
        ("GET", "/api/conversations/501/messages", {}),
        ("GET", "/api/conversations/abc/messages", {}),
    ],
)
def test_unauthenticated_requests_get_401_before_anything_else(store, method, path, kwargs):
    response = call(method, path, **kwargs)
    assert response.status_code == 401
    assert error_of(response) == {"code": "unauthenticated", "message": "로그인이 필요합니다.", "fields": {}}
    assert (store.calls, store.commits) == (0, 0)


def test_create_stores_a_conversation_for_the_verified_user(store):
    sign_in(101)
    response = call("POST", "/api/conversations", json={})
    assert response.status_code == 201
    assert response.json() == {
        "conversation": {"id": "601", "created_at": "2026-01-02T03:50:00Z", "updated_at": "2026-01-02T03:50:00Z"}
    }
    assert store.conversations[-1].user_id == 101
    assert store.commits == 1


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"json": {"user_id": "202"}}, "user_id"),
        ({"json": {"title": "x"}}, "title"),
        ({"json": []}, "body"),
        ({"content": b"{", "headers": {"Content-Type": "application/json"}}, "body"),
        ({}, "body"),
    ],
    ids=["user_id field", "unknown field", "not an object", "malformed JSON", "no body"],
)
def test_create_rejects_any_body_except_an_empty_object(store, kwargs, field):
    sign_in(101)
    response = call("POST", "/api/conversations", **kwargs)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert set(error["fields"]) == {field}
    assert (store.calls, store.commits) == (0, 0)


def test_list_returns_only_my_conversations_newest_first(store):
    sign_in(101)
    response = call("GET", "/api/conversations")
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"id": str(BIG_ID), "created_at": "2026-01-02T03:40:00Z", "updated_at": "2026-01-02T03:40:00Z"},
            {"id": "503", "created_at": "2026-01-02T03:30:00Z", "updated_at": "2026-01-02T03:30:00Z"},
            {"id": "501", "created_at": "2026-01-02T03:05:00Z", "updated_at": "2026-01-02T03:07:00Z"},
        ],
        "next_cursor": None,
    }
    sign_in(202)
    assert ids(call("GET", "/api/conversations")) == ["502"]


def test_list_pages_by_before_id_without_gaps_or_repeats(store):
    sign_in(101)
    first = call("GET", "/api/conversations?limit=2")
    assert ids(first) == [str(BIG_ID), "503"]
    assert first.json()["next_cursor"] == "503"
    second = call("GET", "/api/conversations?limit=2&before_id=503")
    assert ids(second) == ["501"]
    assert second.json()["next_cursor"] is None
    # A page that ends exactly at the last row has no next page.
    assert call("GET", "/api/conversations?limit=3").json()["next_cursor"] is None


def test_list_is_empty_for_a_user_without_conversations(store):
    sign_in(303)
    response = call("GET", "/api/conversations")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_history_returns_messages_in_order_with_the_public_fields(store):
    sign_in(101)
    response = call("GET", "/api/conversations/501/messages")
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert body["items"][:2] == [
        {
            "id": "9001",
            "role": "user",
            "status": "completed",
            "content": "안녕?",
            "request_key": "q-demo-1",
            "reply_to_message_id": None,
            "attempt_no": None,
            "error_code": None,
            "created_at": "2026-01-02T03:06:00Z",
            "updated_at": "2026-01-02T03:06:00Z",
            "completed_at": "2026-01-02T03:06:00Z",
        },
        {
            "id": "9002",
            "role": "assistant",
            "status": "completed",
            "content": "안녕하세요.",
            "request_key": None,
            "reply_to_message_id": "9001",
            "attempt_no": 1,
            "error_code": None,
            "created_at": "2026-01-02T03:06:00Z",
            "updated_at": "2026-01-02T03:07:00Z",
            "completed_at": "2026-01-02T03:07:00Z",
        },
    ]
    assert ids(response) == ["9001", "9002", "9003", "9004"]
    failed = body["items"][3]
    assert (failed["status"], failed["content"], failed["error_code"], failed["completed_at"]) == (
        "failed",
        None,
        "provider_failed",
        None,
    )
    assert "retry_key" not in failed and "conversation_id" not in failed


def test_history_pages_by_after_id_without_gaps_or_repeats(store):
    sign_in(101)
    first = call("GET", "/api/conversations/501/messages?limit=3")
    assert ids(first) == ["9001", "9002", "9003"]
    assert first.json()["next_cursor"] == "9003"
    second = call("GET", "/api/conversations/501/messages?limit=3&after_id=9003")
    assert ids(second) == ["9004"]
    assert second.json()["next_cursor"] is None
    assert call("GET", "/api/conversations/503/messages").json() == {"items": [], "next_cursor": None}


def test_another_users_conversation_looks_exactly_like_a_missing_one(store):
    sign_in(202)
    others = call("GET", "/api/conversations/501/messages")
    missing = call("GET", "/api/conversations/999/messages")
    assert others.status_code == missing.status_code == 404
    assert error_of(others) == error_of(missing) == {"code": "not_found", "message": "찾을 수 없습니다.", "fields": {}}
    assert call("GET", "/api/conversations/502/messages").status_code == 200
    sign_in(101)
    assert call("GET", "/api/conversations/502/messages").status_code == 404


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("/api/conversations?limit=0", "limit"),
        ("/api/conversations?limit=101", "limit"),
        ("/api/conversations?limit=abc", "limit"),
        ("/api/conversations?before_id=abc", "before_id"),
        ("/api/conversations?before_id=0", "before_id"),
        ("/api/conversations?before_id=-1", "before_id"),
        ("/api/conversations?before_id=1.0", "before_id"),
        ("/api/conversations?before_id=9223372036854775808", "before_id"),
        ("/api/conversations/501/messages?limit=101", "limit"),
        ("/api/conversations/501/messages?after_id=007", "after_id"),
        ("/api/conversations/abc/messages", "conversation_id"),
        ("/api/conversations/0/messages", "conversation_id"),
        ("/api/conversations/9223372036854775808/messages", "conversation_id"),
    ],
)
def test_invalid_ids_and_page_inputs_get_422(store, path, field):
    sign_in(101)
    response = call("GET", path)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert set(error["fields"]) == {field}
    assert store.calls == 0


def test_the_largest_bigint_id_is_accepted(store):
    sign_in(101)
    assert call("GET", "/api/conversations/9223372036854775807/messages").status_code == 404
