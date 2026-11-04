"""Production Uvicorn command, proxy trust and login limits over real loopback TCP."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
from threading import Event, Thread
from unittest.mock import patch

from click.testing import CliRunner
import httpx
import pytest
from uvicorn import Config, Server
from uvicorn.main import main as uvicorn_cli

from app.api.google_cookies import GOOGLE_TRANSACTION_COOKIE_NAME
from app.services.login import InvalidCredentials
from test_login_api import BODY, HEADERS, PATH, login_client as login_client

ROOT = Path(__file__).resolve().parents[2]


def production_config(trusted):
    command = json.loads(
        next(
            line[4:]
            for line in (ROOT / "deploy/backend.Dockerfile").read_text().splitlines()
            if line.startswith("CMD ")
        )
    )
    with patch.dict(os.environ, {"FORWARDED_ALLOW_IPS": trusted, "WEB_CONCURRENCY": "8"}):
        with patch("uvicorn.main.run") as run:
            result = CliRunner().invoke(uvicorn_cli, command[1:])
            assert result.exit_code == 0, result.output
        options = dict(run.call_args.kwargs)
        options.pop("app_dir")
        config = Config(*run.call_args.args, **options)
    assert config.workers == 1 and not config.reload and config.proxy_headers
    assert config.forwarded_allow_ips == trusted and not config.access_log
    return config


@contextmanager
def serve(app, trusted):
    ready = Event()

    class ReadyServer(Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    config = production_config(trusted)
    config.app, config.lifespan, config.ws, config.log_config = app, "off", "none", None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = ReadyServer(config)
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            assert ready.wait(10), "HTTP server did not start"
            with httpx.Client(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", trust_env=False, timeout=5
            ) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "HTTP server did not stop"


def attempt(client, index, forwarded):
    return client.post(PATH, json={**BODY, "email": f"fixture-{index}@example.test"}, headers={**HEADERS, **forwarded})


def test_untrusted_peer_cannot_rotate_headers_to_evade_ip_limit(login_client):
    existing, _, authenticate, insert, _, _ = login_client
    authenticate.side_effect = InvalidCredentials()
    with serve(existing.app, "192.0.2.254") as client:
        for index in range(30):
            headers = {
                "X-Forwarded-For": f"198.51.100.{index + 1}",
                "Forwarded": f"for=198.51.100.{index + 1}",
                "X-Real-IP": f"198.51.100.{index + 1}",
            }
            assert attempt(client, index, headers).status_code == 401
        response = attempt(client, 30, {"X-Forwarded-For": "203.0.113.1"})
        assert response.status_code == 429 and response.headers["Retry-After"] == "600"
        assert authenticate.call_count == 30
        insert.assert_not_called()


@pytest.mark.parametrize("trusted", ["127.0.0.1", "127.0.0.0/8"])
def test_trusted_proxy_separates_clients_without_trusting_forged_prefix(login_client, trusted):
    existing, _, authenticate, _, _, clock = login_client
    authenticate.side_effect = InvalidCredentials()
    with serve(existing.app, trusted) as client:
        for index in range(30):
            assert (
                attempt(client, index, {"X-Forwarded-For": f"203.0.113.{index + 1}, 198.51.100.1"}).status_code == 401
            )
        response = attempt(client, 30, {"X-Forwarded-For": "203.0.113.200, 198.51.100.1"})
        assert response.status_code == 429 and response.headers["Retry-After"] == "600"
        assert "set-cookie" not in response.headers
        assert attempt(client, 31, {"X-Forwarded-For": "198.51.100.2"}).status_code == 401
        assert authenticate.call_count == 31
        clock[0] = 600
        assert attempt(client, 32, {"X-Forwarded-For": "198.51.100.1"}).status_code == 401


def test_same_email_is_limited_across_forwarded_ips(login_client):
    existing, _, authenticate, _, _, _ = login_client
    authenticate.side_effect = InvalidCredentials()
    with serve(existing.app, "127.0.0.1") as client:
        for index in range(5):
            response = client.post(PATH, json=BODY, headers={**HEADERS, "X-Forwarded-For": f"198.51.100.{index + 1}"})
            assert response.status_code == 401
        response = client.post(PATH, json=BODY, headers={**HEADERS, "X-Forwarded-For": "198.51.100.99"})
        assert response.status_code == 429
        assert authenticate.call_count == 5


@pytest.mark.parametrize("address", ["198.51.100.1", "2001:db8::1"])
def test_proxy_success_resets_email_but_preserves_ip_failures(login_client, address):
    existing, _, authenticate, _, _, _ = login_client
    headers = {**HEADERS, "X-Forwarded-For": address, "X-Forwarded-Proto": "https"}
    with serve(existing.app, "127.0.0.1") as client:
        authenticate.side_effect = InvalidCredentials()
        for _ in range(4):
            assert client.post(PATH, json=BODY, headers=headers).status_code == 401
        authenticate.side_effect = None
        response = client.post(PATH, json=BODY, headers=headers)
        assert response.status_code == 200 and "set-cookie" in response.headers
        authenticate.side_effect = InvalidCredentials()
        for _ in range(5):
            assert client.post(PATH, json=BODY, headers=headers).status_code == 401
        assert client.post(PATH, json=BODY, headers=headers).status_code == 429
        for index in range(21):
            assert attempt(client, index, {"X-Forwarded-For": address}).status_code == 401
        assert attempt(client, 22, {"X-Forwarded-For": address}).status_code == 429
        assert attempt(client, 22, {"X-Forwarded-For": "198.51.100.2"}).status_code == 401


def test_malformed_forwarded_address_stops_before_credentials(login_client):
    existing, session, authenticate, _, _, _ = login_client
    with serve(existing.app, "127.0.0.1") as client:
        response = attempt(client, 0, {"X-Forwarded-For": "not-an-ip"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "temporarily_unavailable"
        assert "set-cookie" not in response.headers
        authenticate.assert_not_called()
        session.commit.assert_not_called()


@pytest.mark.parametrize("trusted,secure", [("127.0.0.1", True), ("192.0.2.254", False)])
def test_proxy_scheme_reaches_google_cookie_cleanup_fallback(login_client, trusted, secure):
    existing, _, _, _, _, _ = login_client
    existing.app.state.auth_settings = None
    existing.app.state.google_settings = None
    with serve(existing.app, trusted) as client:
        response = client.get("/api/auth/google/callback", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 503 and "location" not in response.headers
        cookies = response.headers.get_list("set-cookie")
        assert len(cookies) == 1 and f"{GOOGLE_TRANSACTION_COOKIE_NAME}=" in cookies[0]
        assert ("Secure" in cookies[0]) is secure
