"""HTTP behavior of the conversation API with an in-memory stand-in for storage.

Every user, ID, body and time is fake. The stand-in applies the owner limit and the
ordering that the repository contract requires; real queries are checked with #8.

Authentication is mocked: the users and the 401 example come from the shared
authentication fixtures, and a test injects an already verified user ID. No test
here performs a login or verifies a token.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

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
    def __init__(self, owner: int, other: int) -> None:
        self.commits = 0
        self.calls = 0
        self.conversations = [
            conversation(501, owner, at(5), at(7)),
            conversation(502, other, at(20), at(21)),
            conversation(503, owner, at(30), at(30)),
            conversation(BIG_ID, owner, at(40), at(40)),
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
def owner(auth_user_a) -> int:
    """The mock verified ID of shared fixture user 101, who owns conversations 501, 503 and BIG_ID."""
    return auth_user_a["principal"]["user_id"]


@pytest.fixture
def other(auth_user_b) -> int:
    """The mock verified ID of shared fixture user 202, who owns conversation 502."""
    return auth_user_b["principal"]["user_id"]


@pytest.fixture
def store(monkeypatch, owner, other):
    fake = FakeStore(owner, other)
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
def test_unauthenticated_requests_get_401_before_anything_else(store, unauthenticated_case, method, path, kwargs):
    expected = unauthenticated_case["expected_response"]["error"]
    response = call(method, path, headers=unauthenticated_case["request_headers"], **kwargs)
    assert response.status_code == unauthenticated_case["expected_status"]
    assert error_of(response) == {name: value for name, value in expected.items() if name != "trace_id"}
    assert (store.calls, store.commits) == (0, 0)


def test_create_stores_a_conversation_for_the_verified_user(store, owner):
    sign_in(owner)
    response = call("POST", "/api/conversations", json={})
    assert response.status_code == 201
    assert response.json() == {
        "conversation": {"id": "601", "created_at": "2026-01-02T03:50:00Z", "updated_at": "2026-01-02T03:50:00Z"}
    }
    assert store.conversations[-1].user_id == owner
    assert store.commits == 1


def test_create_rejects_another_users_id_in_the_body(store, principal_body_mismatch_case):
    """The shared mismatch cases: a body cannot substitute the verified user."""
    sign_in(principal_body_mismatch_case["expected_principal_user_id"])
    response = call("POST", "/api/conversations", json=principal_body_mismatch_case["request_body"])
    assert response.status_code == 422
    assert set(error_of(response)["fields"]) == {"user_id"}
    assert (store.calls, store.commits) == (0, 0)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"json": {"title": "x"}}, "title"),
        ({"json": []}, "body"),
        ({"content": b"{", "headers": {"Content-Type": "application/json"}}, "body"),
        ({}, "body"),
    ],
    ids=["unknown field", "not an object", "malformed JSON", "no body"],
)
def test_create_rejects_any_body_except_an_empty_object(store, owner, kwargs, field):
    sign_in(owner)
    response = call("POST", "/api/conversations", **kwargs)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert set(error["fields"]) == {field}
    assert (store.calls, store.commits) == (0, 0)


def test_list_returns_only_my_conversations_newest_first(store, owner, other):
    sign_in(owner)
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
    sign_in(other)
    assert ids(call("GET", "/api/conversations")) == ["502"]


def test_list_pages_by_before_id_without_gaps_or_repeats(store, owner):
    sign_in(owner)
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


def test_history_returns_messages_in_order_with_the_public_fields(store, owner):
    sign_in(owner)
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


def test_history_pages_by_after_id_without_gaps_or_repeats(store, owner):
    sign_in(owner)
    first = call("GET", "/api/conversations/501/messages?limit=3")
    assert ids(first) == ["9001", "9002", "9003"]
    assert first.json()["next_cursor"] == "9003"
    second = call("GET", "/api/conversations/501/messages?limit=3&after_id=9003")
    assert ids(second) == ["9004"]
    assert second.json()["next_cursor"] is None
    assert call("GET", "/api/conversations/503/messages").json() == {"items": [], "next_cursor": None}


def test_another_users_conversation_looks_exactly_like_a_missing_one(store, owner, other):
    sign_in(other)
    others = call("GET", "/api/conversations/501/messages")
    missing = call("GET", "/api/conversations/999/messages")
    assert others.status_code == missing.status_code == 404
    assert error_of(others) == error_of(missing) == {"code": "not_found", "message": "찾을 수 없습니다.", "fields": {}}
    assert call("GET", "/api/conversations/502/messages").status_code == 200
    sign_in(owner)
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
def test_invalid_ids_and_page_inputs_get_422(store, owner, path, field):
    sign_in(owner)
    response = call("GET", path)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert set(error["fields"]) == {field}
    assert store.calls == 0


def test_the_largest_bigint_id_is_accepted(store, owner):
    sign_in(owner)
    assert call("GET", "/api/conversations/9223372036854775807/messages").status_code == 404


UNAVAILABLE = {"code": "temporarily_unavailable", "message": "잠시 후 다시 시도해 주세요.", "fields": {}}
# Details a database error carries and neither a response nor the server log may repeat.
SECRETS = ("fixture-secret", "fixture-hash", "secret_table", "db.internal")
# The statement and bound values an authentication route would have in flight.
STATEMENT = "INSERT INTO secret_table (email, password_hash) VALUES (%(email)s, %(password_hash)s)"
VALUES = {"email": "a@example.test", "password_hash": "fixture-hash"}
DATABASE_FAILURES = [
    OperationalError(STATEMENT, VALUES, Exception("password=fixture-secret host=db.internal")),
    InterfaceError(STATEMENT, VALUES, Exception("connection to db.internal is closed")),
    PoolTimeoutError("pool of db.internal exhausted, fixture-secret"),
]


def failing(error):
    def fail(*_args, **_kwargs):
        raise error

    return fail


def assert_safe_503(response: httpx.Response, caplog, error: Exception) -> None:
    """The common 503 body, and one log entry with the trace ID and error class; no database details in either."""
    assert response.status_code == 503
    trace_id = response.json()["error"]["trace_id"]
    assert error_of(response) == UNAVAILABLE
    assert not any(secret in response.text for secret in SECRETS)
    (record,) = caplog.records
    assert trace_id in record.getMessage() and type(error).__name__ in record.getMessage()
    # caplog.text is what a log handler writes, including any attached traceback.
    assert record.exc_info is None
    assert not any(secret in caplog.text for secret in (*SECRETS, *VALUES.values()))


@pytest.mark.parametrize("error", DATABASE_FAILURES, ids=["operational", "interface", "pool timeout"])
@pytest.mark.parametrize("path", ["/api/conversations", "/api/conversations/501/messages"], ids=["list", "history"])
def test_database_failure_while_reading_is_a_safe_503(store, owner, monkeypatch, caplog, path, error):
    for name in ("list_conversations", "list_messages"):
        monkeypatch.setattr(repository, name, failing(error))
    sign_in(owner)
    with caplog.at_level(logging.ERROR):
        assert_safe_503(call("GET", path), caplog, error)


@pytest.mark.parametrize("error", DATABASE_FAILURES, ids=["operational", "interface", "pool timeout"])
def test_failed_save_is_a_safe_503_and_never_a_created_response(store, owner, monkeypatch, caplog, error):
    """The row is added, then the commit fails; removing the row is checked against PostgreSQL."""
    monkeypatch.setattr(store, "commit", failing(error))
    sign_in(owner)
    with caplog.at_level(logging.ERROR):
        response = call("POST", "/api/conversations", json={})
        assert_safe_503(response, caplog, error)
    assert "conversation" not in response.json()
    assert (store.calls, store.commits) == (1, 0)


def test_openapi_describes_id_inputs_as_decimal_strings_and_the_common_errors():
    paths = app.openapi()["paths"]
    listing = paths["/api/conversations"]["get"]
    history = paths["/api/conversations/{conversation_id}/messages"]["get"]
    schemas = {item["name"]: item["schema"] for operation in (listing, history) for item in operation["parameters"]}
    for name in ("conversation_id", "before_id", "after_id"):
        # An optional cursor is described as the ID schema or null.
        schema = next(option for option in schemas[name].get("anyOf", [schemas[name]]) if option.get("type") != "null")
        assert schema["type"] == "string"
        assert re.fullmatch(schema["pattern"], "501") and re.fullmatch(schema["pattern"], "9223372036854775807")
        assert not any(re.fullmatch(schema["pattern"], bad) for bad in ("0", "007", "-1", "1.0", "abc", ""))
    error_body = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}}
    assert set(paths["/api/conversations"]["post"]["responses"]) == {"201", "401", "422", "503"}
    assert set(listing["responses"]) == {"200", "401", "422", "503"}
    assert set(history["responses"]) == {"200", "401", "404", "422", "503"}
    for operation, statuses in ((listing, ("401", "422", "503")), (history, ("401", "404", "422", "503"))):
        assert all(operation["responses"][status]["content"] == error_body for status in statuses)
    components = app.openapi()["components"]["schemas"]
    assert components["ConversationOut"]["properties"]["id"]["type"] == "string"
    assert components["MessageOut"]["properties"]["id"]["type"] == "string"
