"""HTTP-level behavior of the existing health and readiness API."""

import asyncio
from unittest.mock import patch

import httpx
import psycopg

from app.main import app


def get(path: str) -> httpx.Response:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.get(path)

    return asyncio.run(request())


def test_health_reports_api_liveness():
    response = get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_query_success():
    with patch("app.main._check_database") as check:
        response = get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    check.assert_called_once_with()


def test_ready_sanitizes_database_failure():
    with patch("app.main._check_database", side_effect=psycopg.OperationalError("secret connection details")):
        response = get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"code": "database_unavailable", "message": "데이터베이스에 연결할 수 없습니다."}
    assert "secret" not in response.text


def test_ready_sanitizes_missing_database_setting():
    with patch.dict("os.environ", {}, clear=True):
        response = get("/api/ready")
    assert response.status_code == 503
    assert "DATABASE_URL" not in response.text
