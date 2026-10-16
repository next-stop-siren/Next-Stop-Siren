"""Sending a question: HTTP behavior and the chat service with an in-memory stand-in for storage.

Every user, ID, body and time is fake, and the provider is a stand-in that makes no
call outside the test. The stand-in storage applies the rules the repository
contract requires; the real statements are checked in test_messages_api_db.py.
Authentication is mocked as in test_conversations_api.py.
"""

import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.api.schemas import MAX_QUESTION_LENGTH, MAX_REQUEST_KEY_LENGTH
from app.db import get_session
from app.integrations.provider import MockProvider, ProviderFailed, ProviderTimeout, get_provider
from app.main import app
from app.repositories import conversations as repository
from app.services import chat
from app.services.chat import ANSWER_TIME_LIMIT_SECONDS, STALE_PENDING_SECONDS
from test_conversations_api import at, call, error_of, failing, message, sign_in

NOW = at(6)
LATER = at(7)
# Created long enough before NOW that recovery closes it.
STALE = NOW - timedelta(seconds=STALE_PENDING_SECONDS + 1)
PATH = "/api/conversations/501/messages"

QUESTION = {
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
}
ANSWER = {
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
}
PENDING = {**ANSWER, "status": "pending", "content": None, "updated_at": QUESTION["created_at"], "completed_at": None}
FAILED = {**ANSWER, "status": "failed", "content": None, "error_code": "provider_failed", "completed_at": None}
INTERRUPTED = {**FAILED, "status": "interrupted", "error_code": "outcome_unknown"}


class Crash(Exception):
    """The process dying during the provider call: nothing after the call runs."""


class FakeChatStore:
    """Conversation 501 of the owner, as the session and the repository functions of the chat service."""

    def __init__(self, owner: int) -> None:
        self.owner = owner
        self.rows: list[SimpleNamespace] = []
        self.commits = 0
        self.calls = 0

    def commit(self) -> None:
        self.commits += 1

    def pending(self) -> list[SimpleNamespace]:
        return [row for row in self.rows if row.role == "assistant" and row.status == "pending"]

    def lock_conversation(self, _session, user_id, conversation_id):
        self.calls += 1
        return (user_id, conversation_id) == (self.owner, 501)

    def close_stale_pending(self, _session, _conversation_id, older_than_seconds):
        stale = [row for row in self.pending() if row.created_at < NOW - timedelta(seconds=older_than_seconds)]
        for row in stale:
            row.status, row.error_code = "interrupted", "outcome_unknown"
            row.updated_at = LATER
        return len(stale)

    def has_pending_answer(self, _session, _conversation_id):
        return bool(self.pending())

    def find_question(self, _session, _conversation_id, request_key):
        return next((row for row in self.rows if row.request_key == request_key), None)

    def find_question_by_id(self, _session, _conversation_id, question_id):
        return next((row for row in self.rows if row.id == question_id and row.role == "user"), None)

    def find_retry(self, _session, question_id, retry_key):
        attempts = (row for row in self.rows if row.reply_to_message_id == question_id)
        return next((row for row in attempts if row.retry_key == retry_key), None)

    def has_later_question(self, _session, _conversation_id, question_id):
        return any(row.role == "user" and row.id > question_id for row in self.rows)

    def latest_answer(self, _session, question_id):
        attempts = [row for row in self.rows if row.reply_to_message_id == question_id]
        return max(attempts, key=lambda row: row.attempt_no)

    def list_context_messages(self, _session, _user_id, _conversation_id):
        return [row for row in self.rows if row.status == "completed"]

    def add_question(self, _session, _conversation_id, content, request_key, created=NOW):
        number = 9001 + len(self.rows)
        self.rows += [
            message(
                number, "user", "completed", created, content=content, request_key=request_key, completed_at=created
            ),
            message(number + 1, "assistant", "pending", created, reply_to_message_id=number, attempt_no=1),
        ]
        return self.rows[-2], self.rows[-1]

    def add_retry(self, _session, _conversation_id, question_id, attempt_no, retry_key, created=NOW):
        fields = {"reply_to_message_id": question_id, "attempt_no": attempt_no, "retry_key": retry_key}
        self.rows.append(message(9001 + len(self.rows), "assistant", "pending", created, **fields))
        return self.rows[-1]

    def finish_answer(self, _session, _conversation_id, answer_id, *, status, content, error_code):
        answer = next(row for row in self.rows if row.id == answer_id)
        if answer.status == "pending":
            answer.status, answer.content, answer.error_code = status, content, error_code
            answer.updated_at = LATER
            answer.completed_at = LATER if status == "completed" else None
        return answer


class FakeProvider:
    """Records each call with what `observe` reports during it, then returns or raises the set outcome."""

    def __init__(self, observe) -> None:
        self.observe = observe
        self.outcome = lambda: "안녕하세요."
        self.calls: list[SimpleNamespace] = []

    def complete(self, messages, *, timeout_seconds):
        context = [(item.role, item.content) for item in messages]
        self.calls.append(SimpleNamespace(context=context, observed=self.observe(), timeout_seconds=timeout_seconds))
        return self.outcome()


@pytest.fixture
def store(monkeypatch, auth_user_a):
    fake = FakeChatStore(auth_user_a["principal"]["user_id"])
    for name in (
        "lock_conversation",
        "close_stale_pending",
        "has_pending_answer",
        "find_question",
        "find_question_by_id",
        "find_retry",
        "has_later_question",
        "latest_answer",
        "list_context_messages",
        "add_question",
        "add_retry",
        "finish_answer",
    ):
        monkeypatch.setattr(repository, name, getattr(fake, name))
    app.dependency_overrides[get_session] = lambda: fake
    sign_in(fake.owner)
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def provider(store):
    """Observes how many commits were made before each provider call."""
    fake = FakeProvider(lambda: store.commits)
    app.dependency_overrides[get_provider] = lambda: fake
    return fake


def send(content="안녕?", request_key="q-demo-1", path=PATH):
    return call("POST", path, json={"content": content, "request_key": request_key})


def stored(store):
    return [(row.role, row.status, row.content, row.error_code) for row in store.rows]


@pytest.mark.parametrize("body", [{"content": "안녕?", "request_key": "q-demo-1"}, {"content": " ", "user_id": "101"}])
def test_unauthenticated_send_gets_401_before_anything_else(store, provider, unauthenticated_case, body):
    app.dependency_overrides.clear()
    response = call("POST", PATH, headers=unauthenticated_case["request_headers"], json=body)
    assert response.status_code == unauthenticated_case["expected_status"]
    assert error_of(response)["code"] == "unauthenticated"
    assert (store.calls, store.commits, provider.calls) == (0, 0, [])


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"content": "", "request_key": "q"}, "content"),
        ({"content": "   ", "request_key": "q"}, "content"),
        ({"content": "\n\t　", "request_key": "q"}, "content"),
        ({"content": "x" * (MAX_QUESTION_LENGTH + 1), "request_key": "q"}, "content"),
        ({"content": "a\x00b", "request_key": "q"}, "content"),
        ({"content": 123, "request_key": "q"}, "content"),
        ({"content": None, "request_key": "q"}, "content"),
        ({"request_key": "q"}, "content"),
        ({"content": "안녕?", "request_key": ""}, "request_key"),
        ({"content": "안녕?", "request_key": "k" * (MAX_REQUEST_KEY_LENGTH + 1)}, "request_key"),
        ({"content": "안녕?", "request_key": "a\x00b"}, "request_key"),
        ({"content": "안녕?", "request_key": 1}, "request_key"),
        ({"content": "안녕?"}, "request_key"),
        # An unknown field's name is not repeated in the response.
        ({"content": "안녕?", "request_key": "q", "user_id": "202"}, "body"),
        ({"content": "안녕?", "request_key": "q", "role": "assistant"}, "body"),
        ([], "body"),
    ],
    ids=[
        "empty question",
        "spaces only",
        "other whitespace only",
        "question too long",
        "NUL in question",
        "question is a number",
        "question is null",
        "no question",
        "empty key",
        "key too long",
        "NUL in key",
        "key is a number",
        "no key",
        "user ID in body",
        "unknown field",
        "not an object",
    ],
)
def test_invalid_question_is_422_and_stores_nothing(store, provider, body, field):
    response = call("POST", PATH, json=body)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert set(error["fields"]) == {field}
    assert (store.calls, store.commits, provider.calls) == (0, 0, [])


def test_invalid_conversation_id_is_422(store, provider):
    response = send(path="/api/conversations/abc/messages")
    assert response.status_code == 422
    assert set(error_of(response)["fields"]) == {"conversation_id"}
    assert store.calls == 0


def test_new_question_is_stored_then_answered_and_returned_as_201(store, provider):
    response = send()
    assert response.status_code == 201
    assert response.json() == {"question": QUESTION, "answer": ANSWER}
    assert stored(store) == [("user", "completed", "안녕?", None), ("assistant", "completed", "안녕하세요.", None)]
    # One call, made after the question and its pending answer were committed and before the outcome was.
    (made,) = provider.calls
    assert made.observed == 1 and store.commits == 2
    assert made.context == [("user", "안녕?")]
    assert made.timeout_seconds == ANSWER_TIME_LIMIT_SECONDS < STALE_PENDING_SECONDS


def test_question_is_stored_exactly_as_sent_up_to_the_length_limit(store, provider):
    content = "  " + "가" * (MAX_QUESTION_LENGTH - 2)
    response = send(content=content, request_key="k" * MAX_REQUEST_KEY_LENGTH)
    assert response.status_code == 201
    assert response.json()["question"]["content"] == content == store.rows[0].content


def test_resending_the_same_key_and_body_returns_the_stored_pair_without_a_second_call(store, provider):
    first = send()
    again = send()
    assert again.status_code == 200
    assert again.json() == first.json() == {"question": QUESTION, "answer": ANSWER}
    assert len(store.rows) == 2 and len(provider.calls) == 1


def test_the_same_key_with_another_body_is_409_and_changes_nothing(store, provider):
    send()
    response = send(content="안녕!")
    assert response.status_code == 409
    assert error_of(response) == {
        "code": "request_key_conflict",
        "message": "같은 요청 키로 다른 질문을 보낼 수 없습니다.",
        "fields": {},
    }
    assert len(store.rows) == 2 and len(provider.calls) == 1


def test_while_an_answer_is_pending_a_resend_is_202_and_another_question_is_409(store, provider):
    store.add_question(None, 501, "안녕?", "q-demo-1")
    resend = send()
    assert resend.status_code == 202
    assert resend.json() == {"question": QUESTION, "answer": PENDING}
    other = send(content="하나 더", request_key="q-demo-2")
    assert other.status_code == 409
    assert error_of(other) == {
        "code": "conversation_busy",
        "message": "이전 질문의 답변을 기다리고 있습니다.",
        "fields": {},
    }
    assert len(store.rows) == 2 and provider.calls == []


@pytest.mark.parametrize(
    ("outcome", "status_code", "code", "answer"),
    [
        (failing(ProviderFailed()), 502, "provider_failed", FAILED),
        (failing(ProviderTimeout()), 504, "provider_timeout", INTERRUPTED),
        (lambda: " ", 502, "provider_failed", FAILED),
    ],
    ids=["failure", "timeout", "empty answer"],
)
def test_provider_failure_is_stored_and_a_resend_shows_it_without_a_second_call(
    store, provider, outcome, status_code, code, answer
):
    provider.outcome = outcome
    response = send()
    assert response.status_code == status_code
    error = error_of(response)
    assert (error["code"], error["fields"]) == (code, {})
    assert "question" not in response.json()
    again = send()
    assert again.status_code == 200
    assert again.json() == {"question": QUESTION, "answer": answer}
    assert len(store.rows) == 2 and len(provider.calls) == 1


def test_a_failed_answer_does_not_block_the_conversation_or_enter_the_next_context(store, provider):
    send(content="하나", request_key="q-1")
    provider.outcome = failing(ProviderFailed())
    assert send(content="둘", request_key="q-2").status_code == 502
    provider.outcome = failing(ProviderTimeout())
    assert send(content="셋", request_key="q-3").status_code == 504
    provider.outcome = lambda: "네 번째 답변"
    assert send(content="넷", request_key="q-4").status_code == 201
    assert provider.calls[-1].context == [
        ("user", "하나"),
        ("assistant", "안녕하세요."),
        ("user", "둘"),
        ("user", "셋"),
        ("user", "넷"),
    ]


def test_after_a_crash_the_pending_answer_stays_until_the_recovery_limit(store, provider):
    provider.outcome = failing(Crash())
    with pytest.raises(Crash):
        send()
    assert stored(store) == [("user", "completed", "안녕?", None), ("assistant", "pending", None, None)]
    provider.outcome = lambda: "안녕하세요."
    assert send().status_code == 202
    assert send(content="하나 더", request_key="q-demo-2").status_code == 409
    assert len(provider.calls) == 1


def test_resending_after_the_recovery_limit_shows_the_answer_as_interrupted(store, provider):
    store.add_question(None, 501, "안녕?", "q-demo-1", created=STALE)
    response = send()
    assert response.status_code == 200
    answer = response.json()["answer"]
    assert (answer["status"], answer["content"], answer["error_code"]) == ("interrupted", None, "outcome_unknown")
    assert provider.calls == [] and store.commits == 1


def test_a_new_question_after_the_recovery_limit_closes_the_old_answer_and_is_answered(store, provider):
    store.add_question(None, 501, "안녕?", "q-demo-1", created=STALE)
    response = send(content="하나 더", request_key="q-demo-2")
    assert response.status_code == 201
    assert stored(store) == [
        ("user", "completed", "안녕?", None),
        ("assistant", "interrupted", None, "outcome_unknown"),
        ("user", "completed", "하나 더", None),
        ("assistant", "completed", "안녕하세요.", None),
    ]


def test_an_answer_that_arrives_after_recovery_closed_it_is_not_stored(store, provider):
    def recovered_meanwhile():
        store.rows[1].created_at = STALE
        store.close_stale_pending(None, 501, STALE_PENDING_SECONDS)
        return "늦은 답변"

    provider.outcome = recovered_meanwhile
    response = send()
    assert response.status_code == 504
    assert error_of(response)["code"] == "provider_timeout"
    assert stored(store)[1] == ("assistant", "interrupted", None, "outcome_unknown")


def test_another_users_conversation_and_a_missing_one_are_the_same_404(store, provider, auth_user_b):
    missing = send(path="/api/conversations/999/messages")
    sign_in(auth_user_b["principal"]["user_id"])
    others = send()
    assert others.status_code == missing.status_code == 404
    assert error_of(others) == error_of(missing) == {"code": "not_found", "message": "찾을 수 없습니다.", "fields": {}}
    assert (store.rows, store.commits, provider.calls) == ([], 0, [])


def test_an_answer_exactly_at_the_recovery_limit_is_still_pending(store, provider):
    store.add_question(None, 501, "안녕?", "q-demo-1", created=NOW - timedelta(seconds=STALE_PENDING_SECONDS))
    assert send().status_code == 202
    assert send(content="하나 더", request_key="q-demo-2").status_code == 409


# Private text a request carries; neither its response nor the server log may repeat it.
PRIVATE_QUESTION = "비밀 질문 fixture-private-question"
PRIVATE_KEY = "fixture-private-key"
PROVIDER_DETAIL = "fixture-provider-secret"


@pytest.mark.parametrize(
    ("error", "status_code", "outcome"),
    [
        (ProviderFailed(PROVIDER_DETAIL), 502, "provider_failed"),
        (ProviderTimeout(PROVIDER_DETAIL), 504, "outcome_unknown"),
    ],
    ids=["failure", "timeout"],
)
def test_provider_failure_response_and_server_log_share_the_request_trace_id(
    store, provider, caplog, error, status_code, outcome
):
    provider.outcome = failing(error)
    with caplog.at_level(logging.INFO, logger="app"):
        response = send(content=PRIVATE_QUESTION, request_key=PRIVATE_KEY)
    assert response.status_code == status_code
    trace_id = response.json()["error"]["trace_id"]
    messages = [record.getMessage() for record in caplog.records]
    # Received, question saved, provider call ended and answer saved, all under the response's trace ID.
    assert [message.split(",")[0] for message in messages] == [
        "Question received",
        "Question saved",
        "Provider call ended",
        "Answer saved",
    ]
    assert all(f"trace_id={trace_id}" in message for message in messages)
    assert f"outcome={outcome}" in messages[2] and caplog.records[2].levelno == logging.WARNING
    assert all(record.exc_info is None for record in caplog.records)
    for private in (PRIVATE_QUESTION, PRIVATE_KEY, PROVIDER_DETAIL):
        assert private not in caplog.text and private not in response.text
    # Another request has its own trace ID.
    conflict = send(content="다른 질문", request_key=PRIVATE_KEY)
    assert conflict.status_code == 409 and conflict.json()["error"]["trace_id"] != trace_id


@pytest.mark.parametrize(
    ("failing_commit", "events_before"),
    [(1, ["Question received"]), (2, ["Question received", "Question saved", "Provider call ended"])],
    ids=["question save", "answer save"],
)
def test_database_failure_503_shares_the_trace_id_of_the_chat_log(
    store, provider, monkeypatch, caplog, failing_commit, events_before
):
    """The 503 and the chat entries logged before the failure carry one trace ID; no statement values leak."""
    statement = "UPDATE messages SET content=%(content)s"
    failure = OperationalError(statement, {"content": PRIVATE_QUESTION}, Exception(f"password={PROVIDER_DETAIL}"))
    commit = store.commit

    def commit_or_fail():
        if store.commits + 1 == failing_commit:
            raise failure
        commit()

    monkeypatch.setattr(store, "commit", commit_or_fail)
    with caplog.at_level(logging.INFO, logger="app"):
        response = send(content=PRIVATE_QUESTION, request_key=PRIVATE_KEY)
    assert response.status_code == 503
    assert error_of(response)["code"] == "temporarily_unavailable"
    trace_id = response.json()["error"]["trace_id"]
    messages = [record.getMessage() for record in caplog.records]
    assert [message.split(",")[0] for message in messages] == [*events_before, "Database unavailable"]
    assert all(f"trace_id={trace_id}" in message for message in messages)
    for private in (PRIVATE_QUESTION, PRIVATE_KEY, PROVIDER_DETAIL, statement):
        assert private not in caplog.text and private not in response.text


def test_the_service_raises_business_errors_and_leaves_http_to_the_api(store, provider):
    def send_directly(conversation_id=501, content="안녕?", request_key="q-demo-1"):
        return chat.send_question(store, provider, store.owner, conversation_id, content, request_key, trace_id="t")

    with pytest.raises(chat.ConversationNotFound):
        send_directly(conversation_id=999)
    store.add_question(None, 501, "안녕?", "q-demo-1")
    with pytest.raises(chat.RequestKeyConflict):
        send_directly(content="안녕!")
    with pytest.raises(chat.ConversationBusy):
        send_directly(request_key="q-demo-2")
    # Nothing in the service module comes from the HTTP layer.
    assert not any(getattr(value, "__module__", "").startswith("app.api") for value in vars(chat).values())


def test_the_default_provider_is_the_free_mock():
    assert isinstance(get_provider(), MockProvider)
    assert MockProvider().complete([], timeout_seconds=ANSWER_TIME_LIMIT_SECONDS).strip()


def test_openapi_describes_the_send_responses():
    send_operation = app.openapi()["paths"]["/api/conversations/{conversation_id}/messages"]["post"]
    assert set(send_operation["responses"]) == {"200", "201", "202", "401", "404", "409", "422", "502", "503", "504"}
    pair = {"application/json": {"schema": {"$ref": "#/components/schemas/QuestionAnswer"}}}
    assert all(send_operation["responses"][status]["content"] == pair for status in ("200", "201", "202"))
    limits = app.openapi()["components"]["schemas"]["QuestionIn"]["properties"]
    assert limits["content"]["maxLength"] == MAX_QUESTION_LENGTH
    assert (limits["request_key"]["minLength"], limits["request_key"]["maxLength"]) == (1, MAX_REQUEST_KEY_LENGTH)


# Manual retry (S13). D4: a failed or interrupted answer of the latest question may be retried.
RETRY_PATH = f"{PATH}/9001/retry"
RETRIED = {**ANSWER, "id": "9003", "content": "다시 만든 답변", "attempt_no": 2}


def retry(retry_key="r-demo-1", path=RETRY_PATH):
    return call("POST", path, json={"retry_key": retry_key})


def attempts(store):
    return [(row.attempt_no, row.retry_key, row.status) for row in store.rows if row.role == "assistant"]


@pytest.fixture
def failed_first(store, provider):
    """A first answer that failed, with the provider then set to answer the retry."""
    provider.outcome = failing(ProviderFailed())
    assert send().status_code == 502
    provider.outcome = lambda: "다시 만든 답변"
    return store


@pytest.mark.parametrize(
    ("error", "first"),
    [(ProviderFailed(), FAILED), (ProviderTimeout(), INTERRUPTED)],
    ids=["failed", "interrupted"],
)
def test_a_failed_or_interrupted_answer_is_retried_as_a_new_attempt(store, provider, error, first):
    provider.outcome = failing(error)
    send()
    provider.outcome = lambda: "다시 만든 답변"
    response = retry()
    assert response.status_code == 201
    assert response.json() == {"question": QUESTION, "answer": RETRIED}
    # The earlier attempt stays as it was, and the failed one does not enter the context.
    assert attempts(store) == [(1, None, first["status"]), (2, "r-demo-1", "completed")]
    assert provider.calls[-1].context == [("user", "안녕?")]
    assert provider.calls[-1].observed == store.commits - 1
    # A plain resend of the question now shows the latest attempt.
    assert send().json()["answer"] == RETRIED


def test_the_retried_question_gets_the_completed_context_before_it(store, provider):
    send(content="하나", request_key="q-1")
    provider.outcome = failing(ProviderFailed())
    send(content="둘", request_key="q-2")
    provider.outcome = lambda: "다시 만든 답변"
    assert retry(path=f"{PATH}/9003/retry").status_code == 201
    assert provider.calls[-1].context == [("user", "하나"), ("assistant", "안녕하세요."), ("user", "둘")]


def test_resending_the_same_retry_key_returns_its_attempt_without_another_call(failed_first, provider):
    first = retry()
    again = retry()
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert len(provider.calls) == 2 and len(attempts(failed_first)) == 2


def test_a_quick_resend_while_the_retry_is_pending_is_202_and_another_key_is_busy(failed_first, provider):
    failed_first.add_retry(None, 501, 9001, 2, "r-demo-1")
    resend = retry()
    assert resend.status_code == 202
    assert resend.json()["answer"]["status"] == "pending"
    other = retry(retry_key="r-demo-2")
    assert other.status_code == 409 and error_of(other)["code"] == "conversation_busy"
    assert len(provider.calls) == 1 and len(attempts(failed_first)) == 2


def test_a_retry_that_fails_again_is_shown_by_its_key_and_can_be_retried_with_a_new_one(failed_first, provider):
    provider.outcome = failing(ProviderTimeout())
    assert retry().status_code == 504
    again = retry()
    assert again.status_code == 200
    assert (again.json()["answer"]["status"], again.json()["answer"]["attempt_no"]) == ("interrupted", 2)
    assert len(provider.calls) == 2
    provider.outcome = lambda: "세 번째 시도"
    third = retry(retry_key="r-demo-2")
    assert third.status_code == 201 and third.json()["answer"]["attempt_no"] == 3
    assert attempts(failed_first) == [
        (1, None, "failed"),
        (2, "r-demo-1", "interrupted"),
        (3, "r-demo-2", "completed"),
    ]


def test_a_completed_answer_is_not_retried(store, provider):
    send()
    response = retry()
    assert response.status_code == 409
    assert error_of(response) == {
        "code": "retry_not_allowed",
        "message": "이 답변은 다시 시도할 수 없습니다. 대화 기록을 확인해 주세요.",
        "fields": {},
    }
    assert len(provider.calls) == 1 and attempts(store) == [(1, None, "completed")]


def test_after_a_retry_completes_a_new_key_is_refused(failed_first, provider):
    assert retry().status_code == 201
    response = retry(retry_key="r-demo-2")
    assert response.status_code == 409 and error_of(response)["code"] == "retry_not_allowed"
    assert len(provider.calls) == 2


def test_a_pending_answer_is_busy_until_recovery_then_retried(store, provider):
    store.add_question(None, 501, "안녕?", "q-demo-1")
    busy = retry()
    assert busy.status_code == 409 and error_of(busy)["code"] == "conversation_busy"
    assert provider.calls == []
    store.rows[1].created_at = STALE
    provider.outcome = lambda: "다시 만든 답변"
    assert retry().status_code == 201
    assert attempts(store) == [(1, None, "interrupted"), (2, "r-demo-1", "completed")]


def test_only_the_latest_question_is_retried(failed_first, provider):
    send(content="하나 더", request_key="q-demo-2")
    response = retry()
    assert response.status_code == 409
    assert error_of(response) == {
        "code": "retry_not_latest",
        "message": "가장 최근 질문만 다시 시도할 수 있습니다. 새 질문으로 보내 주세요.",
        "fields": {},
    }
    assert [row.attempt_no for row in failed_first.rows if row.reply_to_message_id == 9001] == [1]


def test_a_retry_key_used_before_a_later_question_still_returns_its_attempt(failed_first, provider):
    first = retry()
    send(content="하나 더", request_key="q-demo-2")
    again = retry()
    assert again.status_code == 200 and again.json() == first.json()
    assert len(provider.calls) == 3


def test_a_late_result_of_an_earlier_attempt_does_not_overwrite_the_retry(store, provider):
    """The first call outlives the recovery limit; a retry closes it and completes before the late result arrives."""

    def retried_meanwhile():
        store.rows[1].created_at = STALE
        provider.outcome = lambda: "다시 만든 답변"
        assert retry().status_code == 201
        return "늦은 답변"

    provider.outcome = retried_meanwhile
    response = send()
    assert response.status_code == 504 and error_of(response)["code"] == "provider_timeout"
    assert stored(store) == [
        ("user", "completed", "안녕?", None),
        ("assistant", "interrupted", None, "outcome_unknown"),
        ("assistant", "completed", "다시 만든 답변", None),
    ]
    assert send().json()["answer"] == RETRIED


def test_a_late_result_of_the_retry_does_not_overwrite_its_recovery(failed_first, provider):
    def recovered_meanwhile():
        failed_first.rows[2].created_at = STALE
        failed_first.close_stale_pending(None, 501, STALE_PENDING_SECONDS)
        return "늦은 답변"

    provider.outcome = recovered_meanwhile
    response = retry()
    assert response.status_code == 504
    assert attempts(failed_first) == [(1, None, "failed"), (2, "r-demo-1", "interrupted")]
    assert failed_first.rows[2].content is None


def test_another_users_conversation_and_a_message_that_is_not_a_question_are_404(failed_first, provider, auth_user_b):
    answer = retry(path=f"{PATH}/9002/retry")
    missing = retry(path=f"{PATH}/9999/retry")
    sign_in(auth_user_b["principal"]["user_id"])
    others = retry()
    for response in (answer, missing, others):
        assert response.status_code == 404
        assert error_of(response) == {"code": "not_found", "message": "찾을 수 없습니다.", "fields": {}}
    assert len(provider.calls) == 1 and len(attempts(failed_first)) == 1


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        (RETRY_PATH, {"retry_key": ""}, "retry_key"),
        (RETRY_PATH, {"retry_key": "k" * (MAX_REQUEST_KEY_LENGTH + 1)}, "retry_key"),
        (RETRY_PATH, {"retry_key": "a\x00b"}, "retry_key"),
        (RETRY_PATH, {}, "retry_key"),
        (RETRY_PATH, {"retry_key": "r", "content": "다른 질문"}, "body"),
        (f"{PATH}/abc/retry", {"retry_key": "r"}, "message_id"),
    ],
    ids=["empty key", "key too long", "NUL in key", "no key", "unknown field", "bad message ID"],
)
def test_invalid_retry_is_422_and_stores_nothing(store, provider, path, body, field):
    response = call("POST", path, json=body)
    assert response.status_code == 422
    assert set(error_of(response)["fields"]) == {field}
    assert (store.calls, store.commits, provider.calls) == (0, 0, [])


def test_retry_log_carries_the_trace_id_and_no_private_text(failed_first, provider, caplog):
    with caplog.at_level(logging.INFO, logger="app"):
        response = retry(retry_key=PRIVATE_KEY)
    assert response.status_code == 201
    messages = [record.getMessage() for record in caplog.records]
    assert [message.split(",")[0] for message in messages] == [
        "Retry received",
        "Retry saved",
        "Provider call ended",
        "Answer saved",
    ]
    assert PRIVATE_KEY not in caplog.text and "다시 만든 답변" not in caplog.text


def test_the_retry_service_raises_business_errors(failed_first, provider):
    def retry_directly(conversation_id=501, question_id=9001, retry_key="r-demo-1"):
        return chat.retry_answer(
            failed_first, provider, failed_first.owner, conversation_id, question_id, retry_key, trace_id="t"
        )

    with pytest.raises(chat.ConversationNotFound):
        retry_directly(conversation_id=999)
    with pytest.raises(chat.QuestionNotFound):
        retry_directly(question_id=9002)
    retry_directly()
    with pytest.raises(chat.RetryNotAllowed):
        retry_directly(retry_key="r-demo-2")
    assert chat.RETRYABLE_STATUSES == {"failed", "interrupted"}


def test_openapi_describes_the_retry_responses():
    operation = app.openapi()["paths"]["/api/conversations/{conversation_id}/messages/{message_id}/retry"]["post"]
    assert set(operation["responses"]) == {"200", "201", "202", "401", "404", "409", "422", "502", "503", "504"}
    limits = app.openapi()["components"]["schemas"]["RetryIn"]["properties"]["retry_key"]
    assert (limits["minLength"], limits["maxLength"]) == (1, MAX_REQUEST_KEY_LENGTH)
