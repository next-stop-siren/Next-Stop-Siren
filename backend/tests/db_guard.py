"""Refuse database tests unless the target is this checkout's local test service."""

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit


class UnsafeTestDatabase(ValueError):
    pass


def read_settings(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise UnsafeTestDatabase("Local .env is required for database tests")
    settings = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            settings[name.strip()] = value.strip().strip('"').strip("'")
    return settings


def validate_test_database(url: str | None, settings: dict[str, str]) -> str:
    """Require an explicit, exact match to the configured local db-test target."""
    if not url:
        raise UnsafeTestDatabase("Set TEST_DATABASE_URL explicitly for database tests")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        name = unquote(parsed.path.removeprefix("/"))
    except ValueError as exc:
        raise UnsafeTestDatabase("Invalid TEST_DATABASE_URL") from exc
    expected = (settings.get("TEST_DB_USER"), settings.get("TEST_DB_PASSWORD"), settings.get("TEST_DB_NAME"))
    dev = (settings.get("DEV_DB_USER"), settings.get("DEV_DB_PASSWORD"), settings.get("DEV_DB_NAME"))
    for key in ("TEST_DB_USER", "TEST_DB_PASSWORD", "TEST_DB_NAME", "DEV_DB_USER", "DEV_DB_PASSWORD", "DEV_DB_NAME"):
        if key in os.environ and os.environ[key] != settings.get(key):
            raise UnsafeTestDatabase("Database environment conflicts with local .env")
    if not all(expected) or expected == dev or name == settings.get("DEV_DB_NAME"):
        raise UnsafeTestDatabase("Local test and development database settings must be distinct")
    if (
        parsed.scheme not in {"postgresql", "postgres"}
        or host != "127.0.0.1"
        or port != 55433
        or (user, password, name) != expected
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeTestDatabase("TEST_DATABASE_URL must match the dedicated local db-test service")
    if url in {settings.get("DATABASE_URL"), os.environ.get("DATABASE_URL")}:
        raise UnsafeTestDatabase("Development database cannot be used for tests")
    return url


def guarded_url() -> str:
    root = Path(__file__).resolve().parents[2]
    return validate_test_database(os.environ.get("TEST_DATABASE_URL"), read_settings(root / ".env"))
