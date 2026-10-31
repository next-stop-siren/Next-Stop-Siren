"""S18 HTTP/JWT ownership boundaries on the guarded PostgreSQL temporary schema.

Google discovery/token/JWKS HTTP is replaced by google_callback_flow; RSA/OIDC,
the application's JWT verification, cookies and database commits stay real.
The worker is off. Queue claim/finish and the admission interval are controlled
only to prepare stable states; this suite does not prove provider/SSE execution.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.models.chat_job import ChatJob, ChatQueueMember, ConversationHold
from app.models.conversation import Conversation, Message
from app.models.quota import ChatQuota, QuotaReservation
from app.services import chat_queue, quota
from test_auth_chat_db import BASE_URL, ORIGIN, bearer, create_conversation, register_and_login

pytestmark = pytest.mark.db
MODELS = (Conversation, Message, ChatQuota, QuotaReservation, ChatQueueMember, ChatJob, ConversationHold)
LEDGER = quota.MemberQuota()
MISSING = "9223372036854775807"


@pytest.fixture
def boundary_database(committed_google_sessions, monkeypatch):
    factory = committed_google_sessions
    Conversation.metadata.create_all(factory.kw["bind"], tables=[model.__table__ for model in MODELS])
    monkeypatch.setenv("CHAT_WORKER", "off")
    with patch("app.db._session_factory", factory):
        yield factory


@pytest.fixture(params=["email", "google"])
def members(boundary_database, google_callback_flow, request):
    flow = google_callback_flow
    first = flow.client
    if request.param == "email":
        _, login_a = register_and_login(first)
    else:
        callback = first.get("/api/auth/google/callback", params=flow.params)
        assert callback.status_code == 302
        csrf = first.get("/api/auth/csrf")
        assert csrf.status_code == 200
        refreshed = first.post("/api/auth/refresh", headers={**ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]})
        assert refreshed.status_code == 200
        login_a = refreshed.json()
        me = first.get("/api/me", headers=bearer(login_a["access_token"]))
        assert me.status_code == 200
        login_a["user"] = me.json()["user"]

    with TestClient(flow.app, base_url=BASE_URL, client=("192.0.2.2", 123)) as second:
        # In the Google case these accounts have the same email, but distinct identities.
        credentials = {"email": "fixture@example.test", "password": "fixture-local-password"}
        assert second.post("/api/auth/register", json=credentials, headers=ORIGIN).status_code == 201
        logged_in = second.post("/api/auth/login", json=credentials, headers=ORIGIN)
        assert logged_in.status_code == 200
        login_b = logged_in.json()
        assert login_a["user"]["id"] != login_b["user"]["id"]
        users = []
        for client, login in ((first, login_a), (second, login_b)):
            token = login["access_token"]
            conversation = create_conversation(client, token)
            accepted = client.post(
                f"/api/conversations/{conversation}/messages",
                headers=bearer(token),
                json={"content": f"합성 질문 {login['user']['id']}", "request_key": "same-member-scoped-key"},
            )
            assert accepted.status_code == 202 and accepted.json()["answer"] is None
            users.append(
                SimpleNamespace(
                    client=client,
                    token=token,
                    user_id=int(login["user"]["id"]),
                    conversation=conversation,
                    job=accepted.json()["job"]["id"],
                    question=accepted.json()["question"]["id"],
                )
            )
        yield SimpleNamespace(factory=boundary_database, users=users)


def database_state(factory):
    """Read every chat/queue/ledger column in a fresh session, with no authentication secrets."""
    with factory() as reader:
        return {
            model.__tablename__: reader.execute(select(model.__table__).order_by(*model.__table__.primary_key)).all()
            for model in MODELS
        }


def protected_requests(conversation, question, job):
    path = f"/api/conversations/{conversation}/messages"
    return [
        ("POST", "/api/conversations", {}),
        ("GET", "/api/conversations", None),
        ("GET", path, None),
        ("POST", path, {"content": "거부할 합성 질문", "request_key": "denied"}),
        ("GET", "/api/chat/jobs", None),
        ("GET", f"/api/chat/jobs?conversation_id={conversation}", None),
        ("GET", f"/api/chat/jobs/{job}", None),
        ("GET", "/api/chat/limits", None),
        ("GET", "/api/chat/events", None),
        ("POST", f"/api/chat/jobs/{job}/cancel", {}),
        ("POST", f"{path}/{question}/retry", {"retry_key": "denied"}),
        ("POST", f"{path}/{question}/skip", {}),
    ]


@pytest.mark.parametrize("credential", ["missing", "tampered", "expired", "refresh-as-access"])
def test_all_routes_reject_invalid_http_credentials_without_chat_db_changes(members, credential):
    owner = members.users[0]
    token = owner.token
    if credential == "tampered":
        unsigned, signature = token.rsplit(".", 1)
        token = unsigned + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    elif credential == "refresh-as-access":
        token = owner.client.cookies["refresh_token"]
    headers = {} if credential == "missing" else bearer(token)
    assert owner.client.cookies.get("refresh_token")
    before = database_state(members.factory)

    # Advance only PyJWT's verification clock: the HTTP-issued access really expires,
    # and its signature/claims are still checked by the unchanged verifier.
    class VerificationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=16)

    with patch("jwt.api_jwt.datetime", VerificationClock if credential == "expired" else datetime):
        for method, path, body in protected_requests(owner.conversation, owner.question, owner.job):
            response = owner.client.request(method, path, headers=headers, json=body)
            assert response.status_code == 401, (method, path, response.status_code)
            assert response.json()["error"]["code"] == "unauthenticated"
            assert "text/event-stream" not in response.headers.get("content-type", "")
            assert database_state(members.factory) == before, (method, path)


def prepare_outcome(members, owner, state):
    if state == "queued":
        return
    with members.factory() as session:
        claimed, _ = chat_queue.claim_next(session, LEDGER, owner.user_id)
        assert claimed is not None and str(claimed.job_id) == owner.job
        session.commit()
        if state != "running":
            outcome = chat_queue.PROVIDER_FAILED if state == "failed" else chat_queue.Outcome("completed", "합성 답변")
            assert chat_queue.finish(session, LEDGER, claimed.job_id, claimed.run_token, outcome) is not None
            session.commit()


@pytest.mark.parametrize("state", ["queued", "running", "failed", "completed"])
def test_foreign_resources_match_missing_in_both_directions_and_preserve_all_chat_rows(members, state):
    for owner in members.users:
        prepare_outcome(members, owner, state)
    before = database_state(members.factory)
    for actor, foreign in (members.users, members.users[::-1]):
        foreign_calls = protected_requests(foreign.conversation, foreign.question, foreign.job)
        missing_calls = protected_requests(MISSING, MISSING, MISSING)
        # These endpoints select the authenticated member rather than a resource ID.
        skip_paths = {"/api/conversations", "/api/chat/jobs", "/api/chat/limits", "/api/chat/events"}
        for (method, path, body), (_, missing_path, missing_body) in zip(foreign_calls, missing_calls, strict=True):
            if path in skip_paths:
                continue
            denied = actor.client.request(method, path, headers=bearer(actor.token), json=body)
            absent = actor.client.request(method, missing_path, headers=bearer(actor.token), json=missing_body)
            assert denied.status_code == absent.status_code == 404, (method, path)
            denied_error, absent_error = denied.json()["error"], absent.json()["error"]
            # Each request has its own trace ID; all public error details must match.
            assert UUID(denied_error.pop("trace_id"))
            assert UUID(absent_error.pop("trace_id"))
            assert denied_error == absent_error
            assert denied_error["code"] == "not_found"
            assert database_state(members.factory) == before, (method, path)
        for action in ("retry", "skip"):
            path = f"/api/conversations/{actor.conversation}/messages/{foreign.question}/{action}"
            body = {"retry_key": "mixed-owner"} if action == "retry" else {}
            response = actor.client.post(path, headers=bearer(actor.token), json=body)
            assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
            assert database_state(members.factory) == before


def test_member_reads_and_cookie_bearer_mismatch_use_only_the_verified_member(members):
    for actor, other in (members.users, members.users[::-1]):
        headers = bearer(actor.token)
        conversations = actor.client.get("/api/conversations?limit=1", headers=headers)
        assert conversations.status_code == 200
        assert [row["id"] for row in conversations.json()["items"]] == [actor.conversation]
        messages = actor.client.get(f"/api/conversations/{actor.conversation}/messages", headers=headers)
        assert messages.status_code == 200
        assert [row["id"] for row in messages.json()["items"]] == [actor.question]
        for path in ("/api/chat/jobs", f"/api/chat/jobs?conversation_id={actor.conversation}&limit=1"):
            response = actor.client.get(path, headers=headers)
            assert response.status_code == 200
            assert [row["id"] for row in response.json()["items"]] == [actor.job]
        found = actor.client.get(f"/api/chat/jobs/{actor.job}", headers=headers)
        assert found.status_code == 200
        assert found.json()["question"]["id"] == actor.question
        assert found.json()["job"]["conversation_id"] == actor.conversation
        limits = actor.client.get("/api/chat/limits", headers=headers)
        assert limits.status_code == 200
        assert limits.json()["daily"]["reserved"] == limits.json()["six_hour"]["reserved"] == 1
    for actor, other in (members.users, members.users[::-1]):
        # The actor's cookies name A; only the supplied B Bearer chooses the owner.
        created = actor.client.post("/api/conversations", headers=bearer(other.token), json={})
        assert created.status_code == 201
        with members.factory() as reader:
            row = reader.get(Conversation, int(created.json()["conversation"]["id"]))
            assert row.user_id == other.user_id


def allow_next_admission(factory, user_id):
    with factory() as session:
        row = session.get(ChatQuota, user_id)
        row.last_reserved_at -= timedelta(seconds=11)
        session.commit()


def test_conversation_message_and_job_pages_never_include_the_other_member(members):
    for actor in members.users:
        headers = bearer(actor.token)
        extra = create_conversation(actor.client, actor.token)
        for index in range(2):
            allow_next_admission(members.factory, actor.user_id)
            response = actor.client.post(
                f"/api/conversations/{actor.conversation}/messages",
                headers=headers,
                json={"content": f"페이지 합성 질문 {index}", "request_key": f"page-{index}"},
            )
            assert response.status_code == 202
        actor.conversation_ids = {actor.conversation, extra}

    for actor, other in (members.users, members.users[::-1]):
        headers = bearer(actor.token)
        pages = [
            ("/api/conversations", "before_id", actor.conversation_ids),
            (f"/api/conversations/{actor.conversation}/messages", "after_id", None),
            ("/api/chat/jobs", "before_id", None),
        ]
        for path, cursor_name, expected in pages:
            seen = []
            cursor = None
            for _ in range(5):
                params = {"limit": 1}
                if path == "/api/chat/jobs":
                    params["conversation_id"] = actor.conversation
                if cursor is not None:
                    params[cursor_name] = cursor
                response = actor.client.get(path, headers=headers, params=params)
                assert response.status_code == 200
                body = response.json()
                assert len(body["items"]) == 1
                for item in body["items"]:
                    seen.append(item["id"])
                    if "conversation_id" in item:
                        assert item["conversation_id"] == actor.conversation
                cursor = body["next_cursor"]
                if cursor is None:
                    break
            assert cursor is None
            assert len(seen) == len(set(seen))
            if expected is not None:
                assert set(seen) == expected
                assert not set(seen) & other.conversation_ids
            else:
                model = ChatJob if path.startswith("/api/chat/jobs") else Message
                with members.factory() as reader:
                    expected_ids = reader.scalars(
                        select(model.id).where(model.conversation_id == int(actor.conversation))
                    )
                    assert set(seen) == {str(value) for value in expected_ids}


def test_owner_can_cancel_retry_and_skip_with_http_issued_access(members):
    for actor in members.users:
        headers = bearer(actor.token)
        canceled = actor.client.post(f"/api/chat/jobs/{actor.job}/cancel", headers=headers, json={})
        assert canceled.status_code == 200 and canceled.json()["job"]["state"] == "canceled"
        allow_next_admission(members.factory, actor.user_id)
        path = f"/api/conversations/{actor.conversation}/messages"
        question = actor.client.post(path, headers=headers, json={"content": "실패 준비", "request_key": uuid4().hex})
        assert question.status_code == 202
        actor.job, actor.question = question.json()["job"]["id"], question.json()["question"]["id"]
        prepare_outcome(members, actor, "failed")
        allow_next_admission(members.factory, actor.user_id)
        paused = actor.client.post(path, headers=headers, json={"content": "정지된 후속 질문", "request_key": "paused"})
        assert paused.status_code == 202 and paused.json()["job"]["state"] == "paused"
        paused_id = paused.json()["job"]["id"]
        canceled_paused = actor.client.post(f"/api/chat/jobs/{paused_id}/cancel", headers=headers, json={})
        assert canceled_paused.status_code == 200 and canceled_paused.json()["job"]["state"] == "canceled"
        actions = f"{path}/{actor.question}"
        skipped = actor.client.post(f"{actions}/skip", headers=headers, json={})
        assert skipped.status_code == 200 and skipped.json()["skipped_message_id"] == actor.question
        allow_next_admission(members.factory, actor.user_id)
        retried = actor.client.post(f"{actions}/retry", headers=headers, json={"retry_key": "owner-retry"})
        assert retried.status_code == 202 and retried.json()["job"]["kind"] == "retry"
        retry_id = retried.json()["job"]["id"]
        canceled_retry = actor.client.post(f"/api/chat/jobs/{retry_id}/cancel", headers=headers, json={})
        assert canceled_retry.status_code == 200 and canceled_retry.json()["job"]["state"] == "canceled"
        with members.factory() as reader:
            job = reader.get(ChatJob, int(retry_id))
            assert job.user_id == actor.user_id
            assert reader.get(QuotaReservation, job.reservation_id).state == "refunded"
