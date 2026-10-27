"""Real Argon2, isolated commits and transport disconnects under signup admission limits."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import socket
from threading import Event, Lock, Thread
from unittest.mock import patch
from uuid import uuid4

from argon2 import extract_parameters
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from uvicorn import Config, Server
from uvicorn.protocols.http.h11_impl import H11Protocol

from app.main import create_app
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.security.passwords import PasswordHashError, hash_password, verify_password
from app.security.settings import RegistrationLimitSettings

pytestmark = pytest.mark.db
PATH = "/api/auth/register"
PASSWORD = "fixture-only-password"


class HashProbe:
    """Hold a bounded real-hash call interval, then execute the unchanged Argon2 function."""

    def __init__(self, expected):
        self.expected = expected
        self.entered = Event()
        self.release = Event()
        self.lock = Lock()
        self.active = self.peak = self.calls = 0

    def __call__(self, password):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == self.expected:
                self.entered.set()
        try:
            assert self.release.wait(timeout=15)
            return hash_password(password)
        finally:
            with self.lock:
                self.active -= 1


def body(email=None):
    return {"email": email or f"protection-{uuid4().hex}@example.test", "password": PASSWORD}


def assert_counts(sessions, expected):
    with sessions() as reader:
        assert reader.scalar(select(func.count()).select_from(User)) == expected
        assert reader.scalar(select(func.count()).select_from(RefreshSession)) == expected


def assert_rejected(response, code, retry_after):
    assert response.status_code == (429 if code == "registration_rate_limited" else 503)
    assert response.json()["error"]["code"] == code
    wait = int(response.headers["Retry-After"])
    if code == "registration_rate_limited":
        assert 1 <= wait <= retry_after
    else:
        assert wait == retry_after
    assert f"{wait}초 후" in response.json()["error"]["message"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers and "access_token" not in response.text
    assert PASSWORD not in response.text


@pytest.mark.parametrize("slots", [1, 3], ids=["approved-default", "configured-three"])
def test_twenty_http_signups_bound_real_hashes_and_persist_only_admissions(
    committed_signup_sessions, auth_settings, slots
):
    settings = RegistrationLimitSettings(max_concurrent=slots)
    application = create_app(auth_settings, registration_limit_settings=settings)
    probe = HashProbe(slots)
    headers = {"Origin": auth_settings.allowed_origins[0]}
    with (
        patch("app.db._session_factory", committed_signup_sessions),
        patch("app.services.registration.hash_password", side_effect=probe),
        TestClient(application) as client,
        ThreadPoolExecutor(max_workers=20) as workers,
    ):
        admitted = [workers.submit(client.post, PATH, json=body(), headers=headers) for _ in range(slots)]
        try:
            assert probe.entered.wait(timeout=10)
            rejected = [workers.submit(client.post, PATH, json=body(), headers=headers) for _ in range(20 - slots)]
            # Excess requests must return before the held hash calls are released.
            for future in rejected:
                assert_rejected(future.result(timeout=10), "registration_busy", settings.busy_retry_seconds)
            assert probe.calls == probe.active == probe.peak == slots
            assert_counts(committed_signup_sessions, 0)
        finally:
            probe.release.set()
        responses = [future.result(timeout=10) for future in admitted]
        assert all(response.status_code == 201 for response in responses)
        assert probe.active == 0
        assert_counts(committed_signup_sessions, slots)
        # Busy requests consumed no quota. All remaining approved admissions still succeed.
        for _ in range(settings.limit - slots):
            assert client.post(PATH, json=body(), headers=headers).status_code == 201
        assert_rejected(client.post(PATH, json=body(), headers=headers), "registration_rate_limited", 600)
        assert probe.calls == settings.limit and probe.peak == slots
    assert_counts(committed_signup_sessions, settings.limit)
    with committed_signup_sessions() as reader:
        for user in reader.scalars(select(User)):
            assert verify_password(PASSWORD, user.password_hash)
            parameters = extract_parameters(user.password_hash)
            assert (parameters.memory_cost, parameters.time_cost, parameters.parallelism) == (65536, 3, 1)
            assert (parameters.salt_len, parameters.hash_len) == (16, 32)


@pytest.mark.parametrize("stage", ["hash", "commit"])
def test_failed_admission_rolls_back_and_releases_slot_without_refunding_quota(
    committed_signup_sessions, auth_settings, stage
):
    application = create_app(auth_settings, registration_limit_settings=RegistrationLimitSettings(limit=2))
    headers = {"Origin": auth_settings.allowed_origins[0]}
    candidate = body()

    def fail_commit(session):
        session.flush()
        raise OperationalError("synthetic commit failure", None, RuntimeError("synthetic DB failure"))

    failure = (
        patch("app.services.registration.hash_password", side_effect=PasswordHashError("synthetic hash failure"))
        if stage == "hash"
        else patch.object(Session, "commit", fail_commit)
    )
    with patch("app.db._session_factory", committed_signup_sessions), TestClient(application) as client:
        with failure:
            response = client.post(PATH, json=candidate, headers=headers)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "temporarily_unavailable"
        assert "set-cookie" not in response.headers and "access_token" not in response.text
        assert "Retry-After" not in response.headers
        assert_counts(committed_signup_sessions, 0)
        assert client.post(PATH, json=candidate, headers=headers).status_code == 201
        assert_counts(committed_signup_sessions, 1)
        assert_rejected(client.post(PATH, json=body(), headers=headers), "registration_rate_limited", 600)


@contextmanager
def serve(application, disconnected):
    """Serve only a loopback ephemeral port; signal transport close and startup without sleeps."""
    ready = Event()

    class DisconnectProtocol(H11Protocol):
        def connection_lost(self, exc):
            super().connection_lost(exc)
            disconnected.set()

    class ReadyServer(Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = ReadyServer(
        Config(application, http=DisconnectProtocol, lifespan="off", log_config=None, access_log=False, ws="none")
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        assert ready.wait(timeout=10)
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def test_disconnected_client_does_not_release_running_hash(committed_signup_sessions, auth_settings):
    application = create_app(auth_settings)
    probe = HashProbe(1)
    disconnected = Event()
    finished = Event()
    client_port = [None]
    candidate = body()
    origin = auth_settings.allowed_origins[0]

    async def observe_first_request(scope, receive, send):
        try:
            await application(scope, receive, send)
        finally:
            if scope.get("client", (None, None))[1] == client_port[0]:
                finished.set()

    with (
        patch("app.db._session_factory", committed_signup_sessions),
        patch("app.services.registration.hash_password", side_effect=probe),
        serve(observe_first_request, disconnected) as port,
    ):
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            client_port[0] = raw.getsockname()[1]
            payload = json.dumps(candidate).encode()
            raw.sendall(
                (
                    f"POST {PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: {origin}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
                ).encode()
                + payload
            )
            assert probe.entered.wait(timeout=10)
            raw.shutdown(socket.SHUT_RDWR)
            raw.close()
            assert disconnected.wait(timeout=5)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                assert_rejected(client.post(PATH, json=body(), headers={"Origin": origin}), "registration_busy", 5)
                assert probe.active == probe.calls == 1
                assert not finished.is_set()
                assert_counts(committed_signup_sessions, 0)
                probe.release.set()
                assert finished.wait(timeout=10)
                assert probe.active == 0
                assert_counts(committed_signup_sessions, 1)
                # A lost response can still have committed. A retry must not create a second account.
                duplicate = client.post(PATH, json=candidate, headers={"Origin": origin})
                assert duplicate.status_code == 409
                assert duplicate.json()["error"]["code"] == "local_email_exists"
                assert "set-cookie" not in duplicate.headers
                assert client.post(PATH, json=body(), headers={"Origin": origin}).status_code == 201
                assert_counts(committed_signup_sessions, 2)
                assert probe.peak == 1
        finally:
            raw.close()
            probe.release.set()
