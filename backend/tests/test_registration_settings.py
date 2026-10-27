"""Registration policy configuration, startup snapshots and isolated failure handling."""

from dataclasses import FrozenInstanceError
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.security.settings import AuthSettings, AuthSettingsError, RegistrationLimitSettings

SETTING_FIELDS = (
    ("AUTH_REGISTER_LIMIT", "limit", 10),
    ("AUTH_REGISTER_WINDOW_SECONDS", "window_seconds", 600),
    ("AUTH_REGISTER_MAX_CONCURRENT", "max_concurrent", 1),
    ("AUTH_REGISTER_BUSY_RETRY_SECONDS", "busy_retry_seconds", 5),
)


@pytest.fixture
def registration_environment(monkeypatch):
    """Do not depend on a developer's registration settings."""
    for name, _, _ in SETTING_FIELDS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_missing_values_use_approved_defaults():
    settings = RegistrationLimitSettings.from_env({})
    for _, attribute, expected in SETTING_FIELDS:
        assert getattr(settings, attribute) == expected
    with pytest.raises(FrozenInstanceError):
        settings.limit = 20


@pytest.mark.parametrize("name,attribute,default", SETTING_FIELDS)
def test_each_limit_can_be_configured_independently(name, attribute, default):
    settings = RegistrationLimitSettings.from_env({name: str(default + 1)})
    assert getattr(settings, attribute) == default + 1
    for other_name, other_attribute, other_default in SETTING_FIELDS:
        if other_name != name:
            assert getattr(settings, other_attribute) == other_default


@pytest.mark.parametrize("name,attribute,default", SETTING_FIELDS)
@pytest.mark.parametrize("value", ["", " ", "0", "-1", "+1", "1.5", "1e2", "true", " 1", "1 ", "１", "1\n"])
def test_explicit_invalid_values_are_rejected_without_echoing_input(name, attribute, default, value):
    with pytest.raises(AuthSettingsError) as caught:
        RegistrationLimitSettings.from_env({name: value})
    assert str(caught.value) == f"{name}: 양의 정수를 지정해 주세요."


def test_unparseable_large_integer_uses_safe_configuration_error():
    with pytest.raises(AuthSettingsError, match="AUTH_REGISTER_LIMIT"):
        RegistrationLimitSettings.from_env({"AUTH_REGISTER_LIMIT": "9" * 5000})


@pytest.mark.parametrize("name,attribute,default", SETTING_FIELDS)
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_injected_settings_cannot_disable_or_bypass_integer_validation(name, attribute, default, value):
    with pytest.raises(AuthSettingsError, match=name):
        RegistrationLimitSettings(**{attribute: value})


def test_explicit_empty_mapping_does_not_read_process_environment(registration_environment):
    registration_environment.setenv("AUTH_REGISTER_LIMIT", "invalid-private-setting")
    assert RegistrationLimitSettings.from_env({}).limit == 10


def test_app_reads_settings_once_and_recreation_applies_changes(registration_environment, auth_settings):
    registration_environment.setenv("AUTH_REGISTER_LIMIT", "12")
    application = create_app(auth_settings)
    registration_environment.setenv("AUTH_REGISTER_LIMIT", "20")
    assert application.state.registration_limit_settings.limit == 12
    assert create_app(auth_settings).state.registration_limit_settings.limit == 20


def test_app_accepts_explicit_settings_without_reading_environment(registration_environment, auth_settings):
    registration_environment.setenv("AUTH_REGISTER_LIMIT", "invalid-private-setting")
    settings = RegistrationLimitSettings(limit=12)
    application = create_app(auth_settings, registration_limit_settings=settings)
    assert application.state.registration_limit_settings is settings


def test_invalid_registration_settings_block_signup_without_disabling_login(
    registration_environment, auth_environment, caplog
):
    registration_environment.setenv("AUTH_REGISTER_LIMIT", "invalid-private-setting")
    # Registration-only settings do not enter the existing authentication parser.
    auth_settings = AuthSettings.from_env({**auth_environment, "AUTH_REGISTER_LIMIT": "invalid-private-setting"})
    application = create_app(auth_settings)
    assert application.state.auth_settings is auth_settings
    assert application.state.registration_limit_settings is None
    with (
        patch("app.api.auth.register_local_user") as register,
        patch("app.db.get_session_factory") as sessions,
        TestClient(application, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client,
    ):
        response = client.post(
            "/api/auth/register",
            json={"email": "fixture@example.test", "password": "fixture-only-password"},
            headers={"Origin": auth_settings.allowed_origins[0]},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "temporarily_unavailable"
        assert "set-cookie" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        register.assert_not_called()
        sessions.assert_not_called()
        assert client.get("/api/health").status_code == 200
        # The login route still validates input instead of rejecting its settings.
        login = client.post("/api/auth/login", json={}, headers={"Origin": auth_settings.allowed_origins[0]})
        assert login.status_code == 422
    assert "AUTH_REGISTER_LIMIT" in caplog.text
    assert "invalid-private-setting" not in caplog.text + response.text
