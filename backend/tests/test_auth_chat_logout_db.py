"""S18 logout/refresh/access contracts while real queued work outlives the client.

Real localhost HTTP/SSE, JWT verification, worker and isolated PostgreSQL commits.
Only Google upstream HTTP and the answer provider are controlled test substitutes.
Closing SSE here simulates a client action; it does not prove frontend cleanup.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta
from threading import Barrier
import time
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models.chat_job import ChatJob
from app.models.conversation import Message
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.security.session_values import create_refresh_digest
from test_auth_chat_boundaries_db import (
    boundary_database as boundary_database,
    database_state,
    members as members,
)
from test_auth_chat_db import ORIGIN, bearer
from test_auth_chat_events_db import (
    limits,
    live_api as live_api,
    reservation_states,
    running_worker,
    subscribe,
)

pytestmark = pytest.mark.db


def login_again(api, members, actor, google_flow):
    with members.factory() as reader:
        user = reader.get(User, actor.user_id)
        email, google = user.email, user.password_hash is None
    if google:
        google_flow.start(code=f"s18-relogin-{uuid4().hex}")
        api.client.cookies.clear()
        api.client.cookies.update(google_flow.client.cookies)
        callback = api.client.get("/api/auth/google/callback", params=google_flow.params)
        assert callback.status_code == 302
        csrf = api.client.get("/api/auth/csrf")
        assert csrf.status_code == 200
        response = api.client.post("/api/auth/refresh", headers={**ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]})
    else:
        response = api.client.post(
            "/api/auth/login",
            headers=ORIGIN,
            json={"email": email, "password": "fixture-only-password"},
        )
    assert response.status_code == 200
    access = response.json()["access_token"]
    me = api.client.get("/api/me", headers=bearer(access))
    assert me.status_code == 200 and me.json()["user"]["id"] == str(actor.user_id)
    return access, httpx.Cookies(api.client.cookies)


def auth_snapshot(factory):
    with factory() as reader:
        return {
            row.id: (row.user_id, row.rotated_from_id, row.revoked_at) for row in reader.scalars(select(RefreshSession))
        }


def current_lineage(factory, refresh_token, settings, rows):
    with factory() as reader:
        current = reader.scalar(
            select(RefreshSession.id).where(
                RefreshSession.token_digest == create_refresh_digest(refresh_token, settings)
            )
        )
    assert current is not None
    lineage = set()
    while current is not None:
        assert current not in lineage
        lineage.add(current)
        current = rows[current][1]
    return lineage


def read_job(api, actor):
    response = api.client.get(f"/api/chat/jobs/{actor.job}", headers=bearer(actor.token))
    assert response.status_code == 200
    return response.json()


def wait_job(api, actor, state):
    deadline = time.monotonic() + 5
    while True:
        body = read_job(api, actor)
        if body["job"]["state"] == state:
            return body
        assert time.monotonic() < deadline, (body["job"]["state"], state)
        time.sleep(0.02)


@pytest.mark.parametrize("timing", ["queued", "running", "completion-race"])
def test_logout_revokes_only_current_lineage_and_preserves_work_and_access(
    members, live_api, google_callback_flow, auth_settings, timing
):
    a, b = members.users
    old_cookies = httpx.Cookies(a.client.cookies)
    # Create a second login of A; logout must not revoke this independent lineage.
    _, independent_cookies = login_again(live_api, members, a, google_callback_flow)
    live_api.client.cookies.clear()
    live_api.client.cookies.update(old_cookies)
    csrf = live_api.client.get("/api/auth/csrf")
    assert csrf.status_code == 200
    logout_headers = {**ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]}
    before_auth = auth_snapshot(members.factory)
    lineage = current_lineage(members.factory, old_cookies["refresh_token"], auth_settings, before_auth)

    with ExitStack() as connection, ExitStack() as work:
        stream = connection.enter_context(subscribe(live_api, a))
        event, snapshot = stream.next()
        assert event == "snapshot"
        since = snapshot["server_time"]
        provider = None
        if timing != "queued":
            provider = work.enter_context(running_worker(live_api, members))
            stream.until(lambda event, data: event == "delta")
            wait_job(live_api, b, "running")
        before_chat = database_state(members.factory)
        before_b = limits(live_api, b)

        def logout():
            return live_api.client.post("/api/auth/logout", headers=logout_headers)

        if timing == "completion-race":
            barrier = Barrier(2)

            def simultaneous_logout():
                barrier.wait(timeout=5)
                return logout()

            def finish():
                barrier.wait(timeout=5)
                provider.gates[f"합성 질문 {a.user_id}"].set()

            with ThreadPoolExecutor(max_workers=2) as pool:
                logout_future = pool.submit(simultaneous_logout)
                finish_future = pool.submit(finish)
                response = logout_future.result(timeout=10)
                finish_future.result(timeout=10)
        else:
            response = logout()
            assert database_state(members.factory) == before_chat
            assert read_job(live_api, a)["job"]["state"] == timing
            assert reservation_states(members.factory, a) == ["reserved"]

        assert response.status_code == 204 and response.content == b""
        cookies = response.headers.get_list("set-cookie")
        for name in ("refresh_token", "csrf_token"):
            assert any(cookie.startswith(f"{name}=") and "Max-Age=0" in cookie for cookie in cookies)
        assert live_api.client.get("/api/auth/csrf").status_code == 401
        assert limits(live_api, b) == before_b
        after_auth = auth_snapshot(members.factory)
        assert after_auth.keys() == before_auth.keys()
        for identifier, previous in before_auth.items():
            if identifier in lineage:
                assert after_auth[identifier][:2] == previous[:2]
                assert after_auth[identifier][2] is not None
                if previous[2] is not None:
                    assert after_auth[identifier][2] == previous[2]
            else:
                assert after_auth[identifier] == previous

        # The client explicitly closes its subscription. Logout does not revoke access.
        connection.close()
        if provider is None:
            provider = work.enter_context(running_worker(live_api, members))
        provider.gates[f"합성 질문 {a.user_id}"].set()
        completed = wait_job(live_api, a, "completed")
        assert reservation_states(members.factory, a) == ["settled"]
        with members.factory() as reader:
            job = reader.get(ChatJob, int(a.job))
            answer = reader.get(Message, job.answer_message_id)
            assert job.state == answer.status == "completed"
            assert answer.content == completed["answer"]["content"]

    # Replaying the old, matching cookie+CSRF pair is an auth failure, not a CSRF failure.
    with httpx.Client(base_url=live_api.client.base_url, cookies=old_cookies, trust_env=False, timeout=5) as stale:
        replay = stale.post("/api/auth/refresh", headers=logout_headers)
        assert replay.status_code == 401 and replay.json()["error"]["code"] == "unauthenticated"
    # Independent A login and B login remain refreshable after A's targeted logout.
    for actor, cookies in ((a, independent_cookies), (b, b.client.cookies)):
        with httpx.Client(base_url=live_api.client.base_url, cookies=cookies, trust_env=False, timeout=5) as other:
            csrf = other.get("/api/auth/csrf")
            assert csrf.status_code == 200
            refreshed = other.post("/api/auth/refresh", headers={**ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]})
            assert refreshed.status_code == 200
            me = other.get("/api/me", headers=bearer(refreshed.json()["access_token"]))
            assert me.status_code == 200 and me.json()["user"]["id"] == str(actor.user_id)

    assert read_job(live_api, a)["answer"] == completed["answer"]

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=16)

    with patch("jwt.api_jwt.datetime", ExpiredClock):
        expired = live_api.client.get(f"/api/chat/jobs/{a.job}", headers=bearer(a.token))
        assert expired.status_code == 401
    denied = live_api.client.get(f"/api/chat/jobs/{a.job}", headers=bearer(b.token))
    assert denied.status_code == 404
    access, _ = login_again(live_api, members, a, google_callback_flow)
    restored = live_api.client.get(f"/api/chat/jobs/{a.job}", headers=bearer(access))
    assert restored.status_code == 200 and restored.json()["answer"] == completed["answer"]
    a.token = access
    with subscribe(live_api, a, since) as reconnected:
        event, snapshot = reconnected.next()
        assert event == "snapshot" and snapshot["items"][0]["answer"] == completed["answer"]
        reconnected.assert_owned(members.factory)
