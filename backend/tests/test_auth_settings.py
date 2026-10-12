"""Authentication configuration checks using isolated, fake environment values."""

import base64
from dataclasses import FrozenInstanceError

import pytest

from app.security.settings import AuthSettings, AuthSettingsError, get_auth_settings

KEY_NAMES = ("AUTH_ACCESS_KEY", "AUTH_REFRESH_KEY", "AUTH_REFRESH_DIGEST_KEY", "AUTH_CSRF_KEY")


def test_development_settings_decode_keys_and_preserve_policy(auth_environment) -> None:
    settings = AuthSettings.from_env(auth_environment)
    assert settings.access_key == bytes([1]) * 32
    assert settings.refresh_key == bytes([2]) * 32
    assert settings.refresh_digest_key == bytes([3]) * 32
    assert settings.csrf_key == bytes([4]) * 32
    assert settings.access_kid == "test-access-v1"
    assert settings.refresh_kid == "test-refresh-v1"
    assert settings.issuer == "fixture-issuer"
    assert settings.audience == "fixture-api"
    assert settings.allowed_origins == ("http://localhost:5173",)
    assert settings.cookie_secure is False
    assert settings.access_ttl_seconds == 900
    assert settings.refresh_ttl_seconds == 1209600
    assert settings.cookie_path == "/api/auth"
    assert settings.cookie_samesite == "lax"
    assert settings.cookie_httponly is True
    with pytest.raises(FrozenInstanceError):
        settings.issuer = "changed"


def test_production_supports_multiple_exact_https_origins(auth_environment) -> None:
    settings = AuthSettings.from_env(
        {
            **auth_environment,
            "AUTH_ENV": "production",
            "AUTH_API_ORIGIN": "https://api.example.test",
            "AUTH_ALLOWED_ORIGINS": "https://app.example.test, https://admin.example.test:8443",
            "AUTH_COOKIE_SECURE": "true",
        }
    )
    assert settings.cookie_secure is True
    assert settings.allowed_origins == ("https://app.example.test", "https://admin.example.test:8443")


@pytest.mark.parametrize(
    "api_host,origins",
    [
        ("localhost", "http://localhost:5173"),
        ("127.0.0.1", "http://127.0.0.1:5173"),
        ("127.0.0.1", "http://localhost:5173,http://127.0.0.1:5173"),
        ("localhost", "http://127.0.0.1:5173,http://localhost:5173"),
    ],
)
def test_development_http_origins_allow_explicit_loopback_hosts(auth_environment, api_host, origins) -> None:
    settings = AuthSettings.from_env(
        {
            **auth_environment,
            "AUTH_API_ORIGIN": f"http://{api_host}:8000",
            "AUTH_ALLOWED_ORIGINS": origins,
        }
    )
    assert settings.cookie_secure is False
    assert settings.allowed_origins == tuple(origin.strip() for origin in origins.split(","))


@pytest.mark.parametrize(
    "name",
    [
        *KEY_NAMES,
        "AUTH_ENV",
        "AUTH_ACCESS_KID",
        "AUTH_REFRESH_KID",
        "AUTH_JWT_ISSUER",
        "AUTH_JWT_AUDIENCE",
        "AUTH_API_ORIGIN",
        "AUTH_ALLOWED_ORIGINS",
        "AUTH_COOKIE_SECURE",
    ],
)
@pytest.mark.parametrize("value", [None, "", " "])
def test_required_settings_have_no_implicit_defaults(auth_environment, name: str, value: str | None) -> None:
    if value is None:
        del auth_environment[name]
    else:
        auth_environment[name] = value
    with pytest.raises(AuthSettingsError) as caught:
        AuthSettings.from_env(auth_environment)
    assert name in str(caught.value)


@pytest.mark.parametrize("name", KEY_NAMES)
@pytest.mark.parametrize("value", ["private-invalid-base64", base64.b64encode(b"short-private-key").decode()])
def test_invalid_keys_do_not_appear_in_errors(auth_environment, name: str, value: str) -> None:
    auth_environment[name] = value
    with pytest.raises(AuthSettingsError) as caught:
        AuthSettings.from_env(auth_environment)
    assert name in str(caught.value)
    assert value not in str(caught.value)
    assert "short-private-key" not in str(caught.value)


@pytest.mark.parametrize("name", KEY_NAMES[1:])
def test_keys_cannot_be_reused_between_purposes(auth_environment, name: str) -> None:
    auth_environment[name] = auth_environment["AUTH_ACCESS_KEY"]
    with pytest.raises(AuthSettingsError, match="서로 달라"):
        AuthSettings.from_env(auth_environment)


def test_secret_values_are_hidden_in_settings_repr(auth_environment) -> None:
    settings = AuthSettings.from_env(auth_environment)
    for name in KEY_NAMES:
        assert auth_environment[name] not in repr(settings)
        assert repr(base64.b64decode(auth_environment[name])) not in repr(settings)


@pytest.mark.parametrize("name", ["AUTH_ACCESS_KID", "AUTH_REFRESH_KID"])
@pytest.mark.parametrize("value", ["private/value", "private value", "x" * 65])
def test_invalid_key_ids_do_not_echo_values(auth_environment, name: str, value: str) -> None:
    with pytest.raises(AuthSettingsError) as caught:
        AuthSettings.from_env({**auth_environment, name: value})
    assert value not in str(caught.value)


@pytest.mark.parametrize("name", ["AUTH_JWT_ISSUER", "AUTH_JWT_AUDIENCE"])
def test_claim_configuration_rejects_control_characters(auth_environment, name: str) -> None:
    with pytest.raises(AuthSettingsError) as caught:
        AuthSettings.from_env({**auth_environment, name: "private\nclaim"})
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("name", ["AUTH_API_ORIGIN", "AUTH_ALLOWED_ORIGINS"])
@pytest.mark.parametrize(
    "value",
    [
        "*",
        "null",
        "ftp://localhost",
        "http://localhost/",
        "http://localhost/path",
        "http://user:private@localhost",
        "http://localhost?",
        "http://localhost#",
        "http://local host",
        "http://localhost:99999",
        "http://localhost:",
        "http://localhost:0",
        "http://*.example.test",
        "http://LOCALHOST",
        "http://bad_host.test",
        "http://localhost\n",
    ],
)
def test_origin_configuration_rejects_non_exact_origins(auth_environment, name: str, value: str) -> None:
    with pytest.raises(AuthSettingsError) as caught:
        AuthSettings.from_env({**auth_environment, name: value})
    assert value not in str(caught.value)


@pytest.mark.parametrize("value", ["http://localhost:5173,", "http://localhost:5173,http://localhost:5173"])
def test_origin_list_rejects_empty_or_duplicate_entries(auth_environment, value: str) -> None:
    with pytest.raises(AuthSettingsError):
        AuthSettings.from_env({**auth_environment, "AUTH_ALLOWED_ORIGINS": value})


@pytest.mark.parametrize(
    "name,value",
    [
        ("AUTH_ENV", "staging"),
        ("AUTH_COOKIE_SECURE", "0"),
        ("AUTH_COOKIE_SECURE", "False"),
        ("AUTH_COOKIE_SECURE", "yes"),
    ],
)
def test_policy_flags_are_explicit(auth_environment, name: str, value: str) -> None:
    with pytest.raises(AuthSettingsError):
        AuthSettings.from_env({**auth_environment, name: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"AUTH_ENV": "production"},
        {"AUTH_ENV": "production", "AUTH_COOKIE_SECURE": "true"},
        {"AUTH_API_ORIGIN": "http://127.0.0.2:8000"},
        {"AUTH_API_ORIGIN": "http://api.example.test"},
        {"AUTH_ALLOWED_ORIGINS": "http://app.example.test"},
        {"AUTH_ALLOWED_ORIGINS": "https://127.0.0.1:5173"},
        {"AUTH_ALLOWED_ORIGINS": "http://127.0.0.2:5173"},
        {"AUTH_ALLOWED_ORIGINS": "http://[::1]:5173"},
        {"AUTH_API_ORIGIN": "https://localhost:8000"},
    ],
)
def test_insecure_cookie_exception_is_limited_to_development_http_loopback(auth_environment, overrides) -> None:
    with pytest.raises(AuthSettingsError):
        AuthSettings.from_env({**auth_environment, **overrides})


def test_environment_loader_can_be_used_without_reading_dotenv(auth_environment, monkeypatch) -> None:
    for name, value in auth_environment.items():
        monkeypatch.setenv(name, value)
    assert get_auth_settings() == AuthSettings.from_env(auth_environment)
    monkeypatch.delenv("AUTH_ACCESS_KEY")
    with pytest.raises(AuthSettingsError, match="AUTH_ACCESS_KEY"):
        get_auth_settings()
