"""HTTP-level behavior of the health and readiness API (#72)."""

import asyncio
import logging
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from app.main import app, create_app
from app.observability import SafeError
from app.security.settings import AuthSettings

READY_DB = ("ok", "ok", (), None)
NOT_READY = "서비스가 아직 요청을 받을 준비가 되지 않았습니다."
DB_UNAVAILABLE = {"code": "database_unavailable", "message": "데이터베이스에 연결할 수 없습니다."}


class FakeWorker:
    def __init__(self, status: str = "ok") -> None:
        self.current = status
        self.stale_after: float | None = None

    def status(self, *, stale_after: float) -> str:
        self.stale_after = stale_after
        return self.current


def get(path: str, application: FastAPI = app) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    return asyncio.run(request())


@pytest.fixture
def ready_app(auth_settings: AuthSettings) -> FastAPI:
    """An app whose settings and worker are ready; each test breaks one part."""
    application = create_app(auth_settings)
    application.state.chat_worker = FakeWorker()
    return application


def test_health_reports_api_liveness():
    response = get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ignores_every_readiness_dependency():
    with patch("app.readiness.check_database") as check:
        response = get("/api/health")
    assert response.json() == {"status": "ok"}
    check.assert_not_called()


def test_ready_needs_database_auth_schema_and_worker(ready_app):
    with patch("app.readiness.check_database", return_value=READY_DB) as check:
        response = get("/api/ready", ready_app)
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    check.assert_called_once_with()
    assert ready_app.state.chat_worker.stale_after == 30.0


def test_database_alone_is_not_ready_without_auth_settings(ready_app):
    ready_app.state.auth_settings = None
    with patch("app.readiness.check_database", return_value=READY_DB):
        response = get("/api/ready", ready_app)
    assert response.status_code == 503
    assert response.json() == {
        "code": "not_ready",
        "message": NOT_READY,
        "checks": {"database": "ok", "auth": "not_configured", "schema": "ok", "worker": "ok"},
    }


def test_invalid_registration_limits_are_not_ready(ready_app):
    ready_app.state.registration_limit_settings = None
    with patch("app.readiness.check_database", return_value=READY_DB):
        response = get("/api/ready", ready_app)
    assert response.status_code == 503
    assert response.json()["checks"]["auth"] == "not_configured"


@pytest.mark.parametrize("schema", ["missing_tables", "missing_columns", "unknown"])
def test_database_alone_is_not_ready_without_product_schema(ready_app, schema):
    with patch("app.readiness.check_database", return_value=("ok", schema, ("chat_jobs",), None)):
        response = get("/api/ready", ready_app)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "not_ready" and body["checks"]["schema"] == schema
    # Table names stay in the server log; the response carries only the status word.
    assert "chat_jobs" not in response.text


@pytest.mark.parametrize("status", ["not_started", "starting", "stopped", "stalled"])
def test_worker_that_is_not_polling_is_not_ready(ready_app, status):
    ready_app.state.chat_worker = FakeWorker(status)
    with patch("app.readiness.check_database", return_value=READY_DB):
        response = get("/api/ready", ready_app)
    assert response.status_code == 503
    assert response.json()["checks"]["worker"] == status


def test_worker_that_never_started_is_reported(ready_app):
    ready_app.state.chat_worker = None
    with patch("app.readiness.check_database", return_value=READY_DB):
        response = get("/api/ready", ready_app)
    assert response.json()["checks"]["worker"] == "not_started"


def test_disabled_worker_is_not_ready(auth_settings, monkeypatch):
    """`CHAT_WORKER=off` is test-only; queued jobs would never run, so the lifespan records it."""
    monkeypatch.setenv("CHAT_WORKER", "off")
    application = create_app(auth_settings)

    async def request() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/api/ready")

    with patch("app.readiness.check_database", return_value=READY_DB):
        response = asyncio.run(request())
    assert response.status_code == 503
    assert response.json()["checks"]["worker"] == "disabled"


def test_ready_keeps_database_code_and_hides_failure_details(ready_app):
    error = SafeError(cause="database_unavailable", error_type="OperationalError", where="readiness.py:1")
    failure = ("unavailable", "unknown", (), error)
    with patch("app.readiness.check_database", return_value=failure):
        response = get("/api/ready", ready_app)
    assert response.status_code == 503
    assert response.json() == {
        **DB_UNAVAILABLE,
        "checks": {"database": "unavailable", "auth": "ok", "schema": "unknown", "worker": "ok"},
    }


def test_ready_sanitizes_real_database_failure(ready_app, monkeypatch, caplog):
    import psycopg

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret-password@db:5432/app")
    error = psycopg.OperationalError("connection to secret-password failed: SELECT secret")
    with patch("app.readiness.psycopg.connect", side_effect=error), caplog.at_level(logging.WARNING):
        response = get("/api/ready", ready_app)
    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"
    assert "secret" not in response.text and "secret" not in caplog.text
    assert "database=unavailable" in caplog.text and "error_type=OperationalError" in caplog.text


def test_ready_sanitizes_missing_database_setting(ready_app):
    with patch.dict("os.environ", {}, clear=True):
        response = get("/api/ready", ready_app)
    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"
    assert response.json()["checks"]["database"] == "not_configured"
    assert "DATABASE_URL" not in response.text


def test_ready_recovers_and_logs_only_changes(ready_app, caplog):
    results = [
        ("unavailable", "unknown", (), None),
        ("unavailable", "unknown", (), None),
        ("ok", "missing_tables", ("chat_jobs",), None),
        READY_DB,
        READY_DB,
    ]
    with patch("app.readiness.check_database", side_effect=results), caplog.at_level(logging.INFO, "app.readiness"):
        statuses = [get("/api/ready", ready_app).status_code for _ in results]
    assert statuses == [503, 503, 503, 200, 200]
    messages = [record.getMessage() for record in caplog.records if record.name == "app.readiness"]
    assert messages == [
        "Service not ready, database=unavailable, auth=ok, schema=unknown, worker=ok",
        "Service not ready, database=ok, auth=ok, schema=missing_tables, worker=ok, missing=chat_jobs",
        "Service ready again",
    ]
