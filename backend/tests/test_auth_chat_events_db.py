"""S18 member isolation through real TCP HTTP/SSE and guarded PostgreSQL.

Reuse HTTP-issued email/Google credentials; only Google upstream HTTP is mocked.
A gated fake provider lets the real worker expose both members' temporary output
and settlement separately. No authentication, event hub or queue is replaced.
"""

from contextlib import ExitStack, contextmanager
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
import uvicorn

from app.models.chat_job import ChatJob
from app.models.conversation import Message
from app.models.quota import QuotaReservation
from app.integrations.provider import ProviderTimeout
from app.security.settings import AuthSettings
from app.services import quota
from app.services.chat_runner import ChatJobWorker
from test_auth_chat_boundaries_db import (
    allow_next_admission,
    boundary_database as boundary_database,
    database_state,
    members as members,
    prepare_outcome,
)
from test_auth_chat_db import ORIGIN, bearer

pytestmark = pytest.mark.db


@pytest.fixture
def live_api(members):
    app = members.users[0].client.app
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, ws="none")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline, "HTTP server did not start"
                time.sleep(0.01)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                yield SimpleNamespace(app=app, client=client)
        finally:
            server.should_exit = True
            thread.join(7)
            assert not thread.is_alive(), "HTTP server did not stop"


class Frames:
    def __init__(self, response, owner):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        self.lines = response.iter_lines()
        self.owner = owner
        self.seen = []

    def next(self):
        event, data = None, None
        for line in self.lines:
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif not line and event is not None:
                frame = (event, data)
                self.seen.append(frame)
                return frame
        raise AssertionError("SSE ended before the expected event")

    def until(self, predicate):
        for _ in range(40):
            frame = self.next()
            if predicate(*frame):
                return frame[1]
        raise AssertionError("Expected event not received")

    def assert_owned(self, factory):
        with factory() as reader:
            ids = {
                str(value) for value in reader.scalars(select(ChatJob.id).where(ChatJob.user_id == self.owner.user_id))
            }
        for event, data in self.seen:
            if event == "snapshot":
                for item in data["items"]:
                    assert item["job"]["id"] in ids
                    assert item["job"]["conversation_id"] == self.owner.conversation
            elif event == "job":
                assert data["job"]["id"] in ids
                assert data["job"]["conversation_id"] == self.owner.conversation
            elif event == "delta":
                assert data["job_id"] in ids


@contextmanager
def subscribe(api, actor, since=None):
    params = {} if since is None else {"since": since}
    with api.client.stream("GET", "/api/chat/events", headers=bearer(actor.token), params=params) as response:
        frames = Frames(response, actor)
        yield frames


def limits(api, actor):
    response = api.client.get("/api/chat/limits", headers=bearer(actor.token))
    assert response.status_code == 200
    return response.json()


def reservation_states(factory, actor):
    with factory() as reader:
        return list(reader.scalars(select(QuotaReservation.state).where(QuotaReservation.user_id == actor.user_id)))


class GatedProvider:
    def __init__(self, users):
        self.gates = {f"합성 질문 {actor.user_id}": threading.Event() for actor in users}

    def stream(self, messages, *, timeout_seconds):
        question = messages[-1].content
        yield f"{question} 임시 출력"
        if not self.gates[question].wait(timeout_seconds):
            raise ProviderTimeout()
        yield " 저장 완료"


@contextmanager
def running_worker(api, members):
    provider = GatedProvider(members.users)
    events = api.app.state.chat_events
    worker = ChatJobWorker(
        members.factory,
        provider,
        quota.MemberQuota(),
        poll_seconds=0.05,
        notify=events.notify,
        on_delta=events.delta,
    )
    events.attach(members.factory, worker)
    worker.start()
    try:
        yield provider
    finally:
        for gate in provider.gates.values():
            gate.set()
        worker.stop(timeout=5)
        events.detach()


def test_concurrent_member_streams_keep_deltas_results_and_settlement_separate(members, live_api):
    a, b = members.users
    with subscribe(live_api, a) as stream_a, subscribe(live_api, b) as stream_b:
        for stream, actor in ((stream_a, a), (stream_b, b)):
            event, snapshot = stream.next()
            assert event == "snapshot"
            assert [item["job"]["id"] for item in snapshot["items"]] == [actor.job]
            assert snapshot["limits"]["daily"]["reserved"] == 1
        since = snapshot["server_time"]  # Even another member's cursor cannot select that member.
        with running_worker(live_api, members) as provider:
            for stream, actor in ((stream_a, a), (stream_b, b)):
                delta = stream.until(lambda event, data: event == "delta")
                assert delta == {"job_id": actor.job, "text": f"합성 질문 {actor.user_id} 임시 출력"}
            before_b = limits(live_api, b)
            provider.gates[f"합성 질문 {a.user_id}"].set()
            completed = stream_a.until(lambda event, data: event == "job" and data["job"]["state"] == "completed")
            settled = stream_a.until(lambda event, data: event == "limits")
            assert settled["daily"]["used"] == settled["six_hour"]["used"] == 1
            assert settled["daily"]["reserved"] == settled["six_hour"]["reserved"] == 0
            assert limits(live_api, b) == before_b
            assert reservation_states(members.factory, a) == ["settled"]
            assert reservation_states(members.factory, b) == ["reserved"]
            with members.factory() as reader:
                answer = reader.get(Message, int(completed["answer"]["id"]))
                assert answer.status == "completed" and answer.content == completed["answer"]["content"]
            provider.gates[f"합성 질문 {b.user_id}"].set()
            stream_b.until(lambda event, data: event == "job" and data["job"]["state"] == "completed")
            stream_b.until(lambda event, data: event == "limits")
        stream_a.assert_owned(members.factory)
        stream_b.assert_owned(members.factory)
        # B's first limits event after the observed delta must follow B's own completion.
        tail = stream_b.seen[next(i for i, frame in enumerate(stream_b.seen) if frame[0] == "delta") + 1 :]
        assert [event for event, _ in tail] == ["delta", "job", "limits"]

    with subscribe(live_api, a, since) as reconnected:
        event, snapshot = reconnected.next()
        assert event == "snapshot"
        assert [item["job"]["id"] for item in snapshot["items"]] == [a.job]
        assert snapshot["items"][0]["answer"]["content"] == completed["answer"]["content"]
        reconnected.assert_owned(members.factory)


def test_cancel_refunds_only_its_member_and_reconnect_snapshot_is_private(members, live_api):
    a, b = members.users
    with subscribe(live_api, a) as stream_a, subscribe(live_api, b) as stream_b:
        for stream in (stream_a, stream_b):
            assert stream.next()[0] == "snapshot"
        before_b = limits(live_api, b)
        for actor, stream in ((a, stream_a), (b, stream_b)):
            canceled = live_api.client.post(f"/api/chat/jobs/{actor.job}/cancel", headers=bearer(actor.token), json={})
            assert canceled.status_code == 200
            event, data = stream.next()
            assert event == "job" and data["job"]["id"] == actor.job and data["job"]["state"] == "canceled"
            event, data = stream.next()
            assert event == "limits" and data["daily"]["reserved"] == data["six_hour"]["reserved"] == 0
            assert reservation_states(members.factory, actor) == ["refunded"]
            if actor is a:
                assert limits(live_api, b) == before_b
                assert reservation_states(members.factory, b) == ["reserved"]
        stream_a.assert_owned(members.factory)
        stream_b.assert_owned(members.factory)


def test_skip_resumes_only_its_members_paused_question(members, live_api):
    for actor in members.users:
        prepare_outcome(members, actor, "failed")
        allow_next_admission(members.factory, actor.user_id)
        response = live_api.client.post(
            f"/api/conversations/{actor.conversation}/messages",
            headers=bearer(actor.token),
            json={"content": "정지된 합성 후속 질문", "request_key": "followup"},
        )
        assert response.status_code == 202 and response.json()["job"]["state"] == "paused"
        actor.followup = response.json()["job"]["id"]
    a, b = members.users
    with subscribe(live_api, a) as stream_a, subscribe(live_api, b) as stream_b:
        for stream, actor in ((stream_a, a), (stream_b, b)):
            event, snapshot = stream.next()
            assert event == "snapshot" and snapshot["items"][0]["job"]["id"] == actor.followup
        before_b = limits(live_api, b)
        for actor, stream in ((a, stream_a), (b, stream_b)):
            skipped = live_api.client.post(
                f"/api/conversations/{actor.conversation}/messages/{actor.question}/skip",
                headers=bearer(actor.token),
                json={},
            )
            assert skipped.status_code == 200
            assert [job["id"] for job in skipped.json()["resumed_jobs"]] == [actor.followup]
            resumed = stream.until(lambda event, data: event == "job" and data["job"]["id"] == actor.followup)
            assert resumed["job"]["state"] == "queued"
            stream.until(lambda event, data: event == "limits")
            with members.factory() as reader:
                assert reader.get(ChatJob, int(actor.followup)).state == "queued"
                if actor is a:
                    assert reader.get(ChatJob, int(b.followup)).state == "paused"
            if actor is a:
                assert limits(live_api, b) == before_b
        stream_a.assert_owned(members.factory)
        stream_b.assert_owned(members.factory)


def test_disconnect_during_delta_keeps_work_and_recovers_only_own_stored_answer(members, live_api):
    actor = members.users[0]
    with ExitStack() as connection:
        stream = connection.enter_context(subscribe(live_api, actor))
        event, snapshot = stream.next()
        assert event == "snapshot"
        since = snapshot["server_time"]
        with running_worker(live_api, members) as provider:
            stream.until(lambda event, data: event == "delta")
            connection.close()
            # The socket is now closed, but generation is still waiting for the provider.
            found = live_api.client.get(f"/api/chat/jobs/{actor.job}", headers=bearer(actor.token))
            assert found.status_code == 200 and found.json()["job"]["state"] == "running"
            provider.gates[f"합성 질문 {actor.user_id}"].set()
            deadline = time.monotonic() + 5
            while True:
                found = live_api.client.get(f"/api/chat/jobs/{actor.job}", headers=bearer(actor.token))
                assert found.status_code == 200
                if found.json()["job"]["state"] == "completed":
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert reservation_states(members.factory, actor) == ["settled"]
            with subscribe(live_api, actor, since) as stream:
                event, snapshot = stream.next()
                assert event == "snapshot"
                assert [item["job"]["id"] for item in snapshot["items"]] == [actor.job]
                assert snapshot["items"][0]["answer"] == found.json()["answer"]
                stream.assert_owned(members.factory)


def test_http_issued_access_expiry_ends_subscription_without_changing_jobs(members, live_api):
    actor = members.users[0]
    live_api.client.cookies.update(actor.client.cookies)
    csrf = live_api.client.get("/api/auth/csrf")
    assert csrf.status_code == 200
    # Shorten issuance only; real JWT verification and the SSE clock remain unchanged.
    with patch.object(AuthSettings, "access_ttl_seconds", 3):
        refreshed = live_api.client.post(
            "/api/auth/refresh", headers={**ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]}
        )
    assert refreshed.status_code == 200
    short_lived = SimpleNamespace(**{**vars(actor), "token": refreshed.json()["access_token"]})
    before = database_state(members.factory)
    with subscribe(live_api, short_lived) as stream:
        assert stream.next()[0] == "snapshot"
        assert list(stream.lines) == []  # EOF at expiry, before the 15-second ping.
    assert not live_api.app.state.chat_events.subscribed(actor.user_id)
    expired = live_api.client.get(f"/api/chat/jobs/{actor.job}", headers=bearer(short_lived.token))
    assert expired.status_code == 401
    assert database_state(members.factory) == before
    with subscribe(live_api, actor) as reconnected:
        event, snapshot = reconnected.next()
        assert event == "snapshot" and snapshot["items"][0]["job"]["state"] == "queued"
        reconnected.assert_owned(members.factory)
