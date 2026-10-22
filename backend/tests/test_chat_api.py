"""The member chat queue API without a database (#53).

The queue, view and quota services are replaced by stand-ins, so these checks cover
the HTTP contract only: authentication first, input checks, how each refusal is
answered, `200`/`202`, the public job and limits shapes, and how the subscription hub
orders and closes events. The rules themselves run on PostgreSQL in
test_chat_api_db.py and test_chat_queue_db.py. Every user, ID and time is fake.
"""

import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api import chat as chat_api
from app.api import chat_events
from app.api.chat_events import CLOSED, MAX_PENDING, ChatEvents, Subscription
from app.api.errors import ApiError
from app.api.schemas import MAX_QUESTION_LENGTH
from app.db import get_session, get_session_factory
from app.main import app
from app.repositories import chat_jobs as job_repository
from app.services import chat, chat_queue, chat_views, quota
from app.services.quota import QuotaLimits, QuotaRejected, WindowLimit
from test_conversations_api import call, error_of, message, sign_in

T0 = datetime(2026, 10, 20, 3, 0, 40, tzinfo=UTC)
QUESTION_PATH = "/api/conversations/501/messages"
RETRY_PATH = f"{QUESTION_PATH}/9003/retry"
SKIP_PATH = f"{QUESTION_PATH}/9003/skip"


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


def job(job_id=7003, state="paused", **fields):
    values = {
        "id": job_id,
        "kind": "question",
        "conversation_id": 501,
        "question_message_id": 9005,
        "answer_message_id": None,
        "state": state,
        "paused_by_message_id": 9003 if state == "paused" else None,
        "accepted_at": T0,
        "started_at": None,
        "finished_at": None,
        "expires_at": T0 + timedelta(minutes=5),
    }
    return SimpleNamespace(**{**values, **fields})


QUESTION_ROW = message(
    9005, "user", "completed", T0, content="전동킥보드 헬멧 미착용 범칙금은?", request_key="k-1", completed_at=T0
)
LIMITS = chat_views.Limits(
    QuotaLimits(
        WindowLimit(400, 13, 1, 386, datetime(2026, 10, 20, 15, tzinfo=UTC)),
        WindowLimit(100, 6, 1, 93, datetime(2026, 10, 20, 9, tzinfo=UTC)),
        T0 + timedelta(seconds=10),
    ),
    chat_queue.QueueCounts(0, 1, 0),
)
LIMITS_JSON = {
    "daily": {"limit": 400, "used": 13, "reserved": 1, "remaining": 386, "resets_at": "2026-10-20T15:00:00Z"},
    "six_hour": {"limit": 100, "used": 6, "reserved": 1, "remaining": 93, "resets_at": "2026-10-20T09:00:00Z"},
    "queue": {"running": 0, "questions_waiting": 1, "questions_max": 3, "retries_waiting": 0, "retries_max": 1},
    "next_attempt_at": "2026-10-20T03:00:50Z",
}
PAUSED_JSON = {
    "id": "7003",
    "kind": "question",
    "conversation_id": "501",
    "question_message_id": "9005",
    "answer_message_id": None,
    "state": "paused",
    "position": None,
    "paused_by_message_id": "9003",
    "error_code": None,
    "accepted_at": "2026-10-20T03:00:40Z",
    "started_at": None,
    "finished_at": None,
    "expires_at": "2026-10-20T03:05:40Z",
    "allowed_actions": ["cancel"],
}


@pytest.fixture
def api(monkeypatch, auth_user_a):
    """Signed in as user 101, with the views stubbed and every admission recorded."""
    session = FakeSession()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: lambda: nullcontext(session)
    sign_in(auth_user_a["principal"]["user_id"])
    state = SimpleNamespace(session=session, admitted=[], published=[])

    def items(_session, _user_id, jobs):
        return [
            chat_views.JobItem(chat_views.JobView(found, None, ("cancel",), None), QUESTION_ROW, None) for found in jobs
        ]

    monkeypatch.setattr(chat_views, "items", items)
    monkeypatch.setattr(chat_views, "limits", lambda *_args: LIMITS)
    monkeypatch.setattr(ChatEvents, "publish", lambda _self, _session, user_id, ids: state.published.append(ids))
    yield state
    app.dependency_overrides.clear()


def admit_with(monkeypatch, outcome):
    def admit(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(chat_queue, "admit_question", admit)
    monkeypatch.setattr(chat_queue, "admit_retry", admit)


def send(content="전동킥보드 헬멧 미착용 범칙금은?", request_key="k-1"):
    return call("POST", QUESTION_PATH, json={"content": content, "request_key": request_key})


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", QUESTION_PATH, {"content": "질문", "request_key": "k"}),
        ("POST", RETRY_PATH, {"retry_key": "r"}),
        ("POST", SKIP_PATH, {}),
        ("GET", "/api/chat/jobs", None),
        ("GET", "/api/chat/jobs/7003", None),
        ("POST", "/api/chat/jobs/7003/cancel", {}),
        ("GET", "/api/chat/limits", None),
        ("GET", "/api/chat/events", None),
    ],
)
def test_every_route_is_401_before_a_session_opens(api, unauthenticated_case, method, path, body):
    app.dependency_overrides.clear()
    opened = []
    app.dependency_overrides[get_session] = lambda: opened.append(1)
    response = call(method, path, headers=unauthenticated_case["request_headers"], json=body)
    assert response.status_code == 401 and error_of(response)["code"] == "unauthenticated"
    assert opened == []


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        (QUESTION_PATH, {"content": " ", "request_key": "k"}, "content"),
        (QUESTION_PATH, {"content": "x" * (MAX_QUESTION_LENGTH + 1), "request_key": "k"}, "content"),
        (QUESTION_PATH, {"content": "질문"}, "request_key"),
        (RETRY_PATH, {"retry_key": ""}, "retry_key"),
        (SKIP_PATH, {"reason": "x"}, "body"),
        ("/api/chat/jobs/abc/cancel", {}, "job_id"),
    ],
)
def test_invalid_input_is_422_and_admits_nothing(api, monkeypatch, path, body, field):
    admit_with(monkeypatch, AssertionError("not admitted"))
    response = call("POST", path, json=body)
    assert response.status_code == 422 and set(error_of(response)["fields"]) == {field}
    assert api.session.commits == 0


def test_a_since_without_a_time_zone_is_422(api):
    response = call("GET", "/api/chat/events?since=2026-10-20T03:00:52")
    assert response.status_code == 422 and set(error_of(response)["fields"]) == {"since"}


def test_a_new_question_is_202_with_the_job_and_limits_and_is_announced(api, monkeypatch):
    admit_with(monkeypatch, chat_queue.Admission(QUESTION_ROW, job(), created=True))
    response = send()
    assert response.status_code == 202
    body = response.json()
    assert body["answer"] is None and body["job"] == PAUSED_JSON and body["limits"] == LIMITS_JSON
    assert body["question"]["id"] == "9005"
    assert api.session.commits == 1 and api.published == [[7003]]


@pytest.mark.parametrize(
    ("state", "status_code"),
    [("queued", 202), ("paused", 202), ("running", 202), ("completed", 200), ("failed", 200), ("canceled", 200)],
)
def test_the_same_key_returns_the_stored_job_200_once_ended_and_announces_nothing(api, monkeypatch, state, status_code):
    admit_with(monkeypatch, chat_queue.Admission(QUESTION_ROW, job(state=state), created=False))
    response = send()
    assert response.status_code == status_code
    assert response.json()["job"]["state"] == state and api.published == []


def test_a_question_stored_before_the_queue_has_no_job(api, monkeypatch):
    admit_with(monkeypatch, chat_queue.Admission(QUESTION_ROW, None, created=False))
    answer = message(9006, "assistant", "completed", T0, content="답", reply_to_message_id=9005, attempt_no=1)
    monkeypatch.setattr(job_repository, "latest_answer", lambda *_args: answer)
    response = send()
    assert response.status_code == 200
    assert response.json()["job"] is None and response.json()["answer"]["id"] == "9006"


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (chat.ConversationNotFound(), 404, "not_found"),
        (chat.QuestionNotFound(), 404, "not_found"),
        (chat.RequestKeyConflict(), 409, "request_key_conflict"),
        (chat_queue.QueueFull(), 409, "queue_full"),
        (chat_queue.RetryQueueFull(), 409, "retry_queue_full"),
        (chat.RetryNotAllowed(), 409, "retry_not_allowed"),
        (chat.NotLatestQuestion(), 409, "retry_not_latest"),
    ],
)
@pytest.mark.parametrize("path", [QUESTION_PATH, RETRY_PATH])
def test_refusals_keep_the_common_error_body(api, monkeypatch, error, status_code, code, path):
    admit_with(monkeypatch, error)
    body = {"content": "질문", "request_key": "k"} if path == QUESTION_PATH else {"retry_key": "r"}
    response = call("POST", path, json=body)
    assert response.status_code == status_code and error_of(response)["code"] == code
    assert "Retry-After" not in response.headers and api.session.commits == 0


@pytest.mark.parametrize(
    ("reason", "seconds", "message_text"),
    [
        ("attempt_too_soon", 5, "잠시 후 다시 보내 주세요."),
        ("quota_exceeded", 21180, "질문 한도를 모두 사용했습니다. 초기화 뒤에 다시 시도해 주세요."),
    ],
)
@pytest.mark.parametrize("path", [QUESTION_PATH, RETRY_PATH])
def test_quota_refusals_are_429_with_retry_after(api, monkeypatch, reason, seconds, message_text, path):
    admit_with(monkeypatch, QuotaRejected(reason, seconds))
    body = {"content": "질문", "request_key": "k"} if path == QUESTION_PATH else {"retry_key": "r"}
    response = call("POST", path, json=body)
    assert response.status_code == 429
    assert error_of(response) == {"code": reason, "message": message_text, "fields": {}}
    assert response.headers["Retry-After"] == str(seconds)


@pytest.mark.parametrize("code", ["quota_exceeded", "attempt_too_soon"])
def test_a_quota_refusal_needs_a_positive_wait(code):
    for wrong in (None, 0, 1.5):
        with pytest.raises(ValueError):
            ApiError(code, retry_after=wrong)  # type: ignore[arg-type]
    assert ApiError(code, retry_after=1).retry_after == 1
    with pytest.raises(ValueError):
        ApiError("queue_full", retry_after=1)


def test_skip_returns_the_resumed_jobs_without_the_skipped_one(api, monkeypatch):
    failed = job(7002, state="failed", question_message_id=9003, answer_message_id=9004)
    resumed = job(7003, state="queued")
    monkeypatch.setattr(chat_queue, "skip", lambda *_args: chat_queue.Changes([7002, 7003]))
    monkeypatch.setattr(job_repository, "owned_jobs", lambda *_args: [failed, resumed])
    monkeypatch.setattr(
        chat_views, "views", lambda _s, _u, jobs: [chat_views.JobView(found, 1, ("cancel",), None) for found in jobs]
    )
    response = call("POST", SKIP_PATH, json={})
    assert response.status_code == 200
    body = response.json()
    assert body["skipped_message_id"] == "9003" and [item["id"] for item in body["resumed_jobs"]] == ["7003"]
    assert body["resumed_jobs"][0]["position"] == 1 and api.published == [[7002, 7003]]


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (chat.ConversationNotFound(), 404, "not_found"),
        (chat.QuestionNotFound(), 404, "not_found"),
        (chat_queue.SkipNotAllowed(), 409, "skip_not_allowed"),
    ],
)
def test_skip_refusals(api, monkeypatch, error, status_code, code):
    monkeypatch.setattr(chat_queue, "skip", lambda *_args: (_ for _ in ()).throw(error))
    response = call("POST", SKIP_PATH, json={})
    assert response.status_code == status_code and error_of(response)["code"] == code


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [(chat_queue.JobNotFound(), 404, "not_found"), (chat_queue.JobNotCancelable(), 409, "job_not_cancelable")],
)
def test_cancel_refusals(api, monkeypatch, error, status_code, code):
    monkeypatch.setattr(chat_queue, "cancel", lambda *_args: (_ for _ in ()).throw(error))
    response = call("POST", "/api/chat/jobs/7003/cancel", json={})
    assert response.status_code == status_code and error_of(response)["code"] == code


def test_openapi_lists_the_queue_routes_and_no_stream_route():
    paths = app.openapi()["paths"]
    assert "/api/conversations/{conversation_id}/messages/stream" not in paths
    for path in (
        "/api/conversations/{conversation_id}/messages/{message_id}/skip",
        "/api/chat/jobs",
        "/api/chat/jobs/{job_id}",
        "/api/chat/jobs/{job_id}/cancel",
        "/api/chat/limits",
        "/api/chat/events",
    ):
        assert path in paths
    send_operation = paths["/api/conversations/{conversation_id}/messages"]["post"]
    assert {"200", "202", "404", "409", "429"} <= set(send_operation["responses"])
    assert not {"201", "502", "504"} & set(send_operation["responses"])


def test_execution_order_runs_a_retry_before_its_conversations_questions_only():
    a1 = job(1, state="queued", conversation_id=501)
    b1 = job(2, state="queued", conversation_id=502)
    a2 = job(3, state="queued", conversation_id=501)
    retry = job(4, state="queued", conversation_id=501, kind="retry")
    order = chat_views.execution_order([a1, b1, a2, retry])
    assert [found.id for found in order] == [2, 4, 1, 3]


# The subscription hub ----------------------------------------------------------


def drain(subscription: Subscription, loop: asyncio.AbstractEventLoop, count: int) -> list:
    async def read():
        return [await subscription.get() for _ in range(count)]

    return loop.run_until_complete(read())


def test_a_subscription_keeps_order_and_closes_when_too_far_behind():
    loop = asyncio.new_event_loop()
    try:
        subscription = Subscription(101, loop)
        for index in range(MAX_PENDING + 3):
            subscription.put(f"frame-{index}")
        received = drain(subscription, loop, MAX_PENDING + 1)
        assert received[:MAX_PENDING] == [f"frame-{index}" for index in range(MAX_PENDING)]
        assert received[-1] is CLOSED
    finally:
        loop.close()


def test_open_queues_the_snapshot_first_and_publishes_reach_only_the_member(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        snapshot = chat_views.Snapshot([], LIMITS, T0)
        monkeypatch.setattr(chat_views, "snapshot", lambda *_args: snapshot)
        monkeypatch.setattr(chat_views, "related_ids", lambda _s, _u, ids: list(ids))
        monkeypatch.setattr(job_repository, "owned_jobs", lambda *_args: [])
        monkeypatch.setattr(chat_views, "items", lambda *_args: [])
        monkeypatch.setattr(chat_views, "limits", lambda *_args: LIMITS)
        events = ChatEvents()
        mine = events.open(FakeSession(), 101, loop, None)
        other = events.open(FakeSession(), 202, loop, None)
        assert mine.next_reset == datetime(2026, 10, 20, 9, tzinfo=UTC)
        events.publish(FakeSession(), 101, [7003])
        events.delta(101, 7003, "조각")
        first, limits, delta = drain(mine.subscription, loop, 3)
        assert first.startswith("event: snapshot\n") and '"server_time":"2026-10-20T03:00:40Z"' in first
        assert limits.startswith("event: limits\n")
        assert delta == 'event: delta\ndata: {"job_id":"7003","text":"조각"}\n\n'
        assert drain(other.subscription, loop, 1)[0].startswith("event: snapshot\n")
        assert other.subscription._queue.empty()
        events.close(mine.subscription)
        assert not events.subscribed(101) and events.subscribed(202)
    finally:
        loop.close()


def test_publishing_without_subscribers_reads_nothing_and_a_failed_publish_does_not_raise(monkeypatch, caplog):
    events = ChatEvents()
    monkeypatch.setattr(chat_views, "related_ids", lambda *_args: pytest.fail("no subscriber, no read"))
    events.publish(FakeSession(), 101, [7003])
    loop = asyncio.new_event_loop()
    try:
        monkeypatch.setattr(chat_views, "snapshot", lambda *_args: chat_views.Snapshot([], LIMITS, T0))
        events.open(FakeSession(), 101, loop, None)

        def broken(*_args):
            raise RuntimeError("db down")

        monkeypatch.setattr(chat_views, "related_ids", broken)
        events.publish(FakeSession(), 101, [7003])
        assert "Chat event publish failed, user_id=101" in caplog.text
    finally:
        loop.close()


def test_the_event_stream_pings_when_idle_ends_on_closed_and_unsubscribes(monkeypatch):
    monkeypatch.setattr(chat_api, "PING_SECONDS", 0.01)

    async def run():
        loop = asyncio.get_running_loop()
        events = ChatEvents()
        monkeypatch.setattr(chat_views, "snapshot", lambda *_args: chat_views.Snapshot([], LIMITS, T0))
        opened = chat_events.Opened(
            events.open(FakeSession(), 101, loop, None).subscription, datetime.now(UTC) + timedelta(hours=1)
        )
        stream = chat_api.event_stream(events, opened, lambda: None)  # type: ignore[arg-type, return-value]
        first = await stream.__anext__()
        ping = await stream.__anext__()
        opened.subscription.put("event: job\ndata: {}\n\n")
        job_frame = await stream.__anext__()
        opened.subscription._closed = False
        opened.subscription._pending = MAX_PENDING
        opened.subscription.put("too far behind")
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        return first, ping, job_frame, events.subscribed(101)

    first, ping, job_frame, still_subscribed = asyncio.run(run())
    assert first.startswith("event: snapshot\n") and ping == ": ping\n\n"
    assert job_frame == "event: job\ndata: {}\n\n" and still_subscribed is False


def test_the_ledger_adapter_returns_reservation_ids(monkeypatch):
    monkeypatch.setattr(quota, "reserve", lambda session, user_id, *, now: SimpleNamespace(id=55))
    monkeypatch.setattr(quota, "settle", lambda session, reservation_id: reservation_id == 55)
    monkeypatch.setattr(quota, "refund", lambda session, reservation_id, reason: reason == "canceled")
    ledger = quota.MemberQuota()
    assert ledger.reserve(None, 101, now=T0) == 55  # type: ignore[arg-type]
    assert ledger.settle(None, 55) and ledger.refund(None, 55, "canceled")  # type: ignore[arg-type]
