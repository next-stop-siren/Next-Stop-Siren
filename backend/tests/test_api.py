"""Focused API smoke checks without a running database or test framework."""

import asyncio
import json
import unittest
from unittest.mock import patch

from app.main import _check_database, app


async def request(path: str) -> tuple[int, dict[str, str]]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "http_version": "1.1",
        },
        receive,
        send,
    )
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(body)


class ApiSmokeTest(unittest.TestCase):
    def test_database_probe_executes_bounded_select(self) -> None:
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://example:example@localhost/example"}):
            with patch("app.main.psycopg.connect") as connect:
                connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (1,)
                _check_database()
        connect.assert_called_once_with(
            "postgresql://example:example@localhost/example",
            connect_timeout=2,
            options="-c statement_timeout=2000",
        )
        cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.execute.assert_called_once_with("SELECT 1")

    def test_health_is_live(self) -> None:
        self.assertEqual(asyncio.run(request("/api/health")), (200, {"status": "ok"}))

    def test_ready_when_database_query_succeeds(self) -> None:
        with patch("app.main._check_database") as check:
            self.assertEqual(asyncio.run(request("/api/ready")), (200, {"status": "ready"}))
            check.assert_called_once_with()

    def test_ready_failure_is_sanitized(self) -> None:
        with patch("app.main._check_database", side_effect=RuntimeError("secret connection string")):
            status, body = asyncio.run(request("/api/ready"))
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "database_unavailable")
        self.assertNotIn("secret connection string", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
