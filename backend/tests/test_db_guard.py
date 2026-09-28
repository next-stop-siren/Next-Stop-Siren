"""The guard rejects dangerous targets before a connection is attempted."""

import pytest

from db_guard import UnsafeTestDatabase, validate_test_database


SETTINGS = {
    "TEST_DB_USER": "test_user", "TEST_DB_PASSWORD": "test_password", "TEST_DB_NAME": "test_name",
    "DEV_DB_USER": "dev_user", "DEV_DB_PASSWORD": "dev_password", "DEV_DB_NAME": "dev_name",
    "DATABASE_URL": "postgresql://dev_user:dev_password@127.0.0.1:55432/dev_name",
}


@pytest.mark.parametrize("url", [
    None,
    "postgresql://dev_user:dev_password@127.0.0.1:55432/dev_name",
    "postgresql://test_user:test_password@example.org:55433/test_name",
    "postgresql://test_user:test_password@localhost:55433/test_name",
    "postgresql://test_user:test_password@127.0.0.1:55432/test_name",
    "postgresql://test_user:test_password@127.0.0.1:55433/dev_name",
    "postgresql://test_user:wrong@127.0.0.1:55433/test_name",
    "postgresql://test_user:test_password@127.0.0.1:55433/test_name?sslmode=disable",
])
def test_unsafe_target_rejected(url):
    with pytest.raises(UnsafeTestDatabase):
        validate_test_database(url, SETTINGS)


def test_exact_local_test_target_allowed():
    url = "postgresql://test_user:test_password@127.0.0.1:55433/test_name"
    assert validate_test_database(url, SETTINGS) == url


def test_conflicting_dev_and_test_settings_rejected():
    settings = dict(SETTINGS, DEV_DB_USER="test_user", DEV_DB_PASSWORD="test_password", DEV_DB_NAME="test_name")
    with pytest.raises(UnsafeTestDatabase):
        validate_test_database("postgresql://test_user:test_password@127.0.0.1:55433/test_name", settings)


def test_environment_override_conflict_rejected(monkeypatch):
    monkeypatch.setenv("TEST_DB_NAME", "different_name")
    with pytest.raises(UnsafeTestDatabase):
        validate_test_database("postgresql://test_user:test_password@127.0.0.1:55433/test_name", SETTINGS)
