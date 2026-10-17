"""Google configuration boundaries with fake credentials and no network I/O."""

import base64
from dataclasses import FrozenInstanceError

import pytest

from app.security.settings import AuthSettings, AuthSettingsError, GoogleSettings, get_google_settings

GOOGLE_NAMES = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GOOGLE_TRANSACTION_KEY")
AUTH_KEYS = ("AUTH_ACCESS_KEY", "AUTH_REFRESH_KEY", "AUTH_REFRESH_DIGEST_KEY", "AUTH_CSRF_KEY")


def test_google_settings_decode_key_and_preserve_callback(google_environment):
    settings = GoogleSettings.from_env(google_environment)
    assert settings.client_id == google_environment["GOOGLE_CLIENT_ID"]
    assert settings.client_secret == google_environment["GOOGLE_CLIENT_SECRET"]
    assert settings.redirect_uri == google_environment["GOOGLE_REDIRECT_URI"]
    assert settings.transaction_key == bytes([5]) * 32
    assert settings.discovery_url == "https://accounts.google.com/.well-known/openid-configuration"
    assert settings.scopes == ("openid", "email")
    with pytest.raises(FrozenInstanceError):
        settings.redirect_uri = "https://changed.example.test"


def test_production_callback_uses_validated_https_api_origin(google_environment):
    values = {
        **google_environment,
        "AUTH_ENV": "production",
        "AUTH_API_ORIGIN": "https://api.example.test",
        "AUTH_ALLOWED_ORIGINS": "https://app.example.test",
        "AUTH_COOKIE_SECURE": "true",
        "GOOGLE_REDIRECT_URI": "https://api.example.test/api/auth/google/callback",
    }
    assert GoogleSettings.from_env(values).redirect_uri == values["GOOGLE_REDIRECT_URI"]


@pytest.mark.parametrize("name", GOOGLE_NAMES)
@pytest.mark.parametrize("value", [None, "", " "])
def test_google_fields_are_required_only_for_google(google_environment, name, value):
    if value is None:
        del google_environment[name]
    else:
        google_environment[name] = value
    with pytest.raises(AuthSettingsError, match=name):
        GoogleSettings.from_env(google_environment)
    assert AuthSettings.from_env(google_environment).api_origin == "http://127.0.0.1:8000"


def test_local_auth_does_not_require_google_credentials(auth_environment):
    assert AuthSettings.from_env(auth_environment).cookie_secure is False
    with pytest.raises(AuthSettingsError, match="GOOGLE_CLIENT_ID"):
        GoogleSettings.from_env(auth_environment)


@pytest.mark.parametrize("value", ["not-a-client", "apps.googleusercontent.com", "x.apps.googleusercontent.com.evil"])
def test_invalid_client_id_is_rejected_without_echo(google_environment, value):
    google_environment["GOOGLE_CLIENT_ID"] = value
    with pytest.raises(AuthSettingsError) as caught:
        GoogleSettings.from_env(google_environment)
    assert "GOOGLE_CLIENT_ID" in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize("name", GOOGLE_NAMES)
@pytest.mark.parametrize("prefix,suffix", [(" ", ""), ("", " "), ("", "\n")])
def test_fields_are_not_silently_trimmed(google_environment, name, prefix, suffix):
    google_environment[name] = prefix + google_environment[name] + suffix
    with pytest.raises(AuthSettingsError, match=name):
        GoogleSettings.from_env(google_environment)


@pytest.mark.parametrize("value", ["fixture secret", "fixture\nsecret", "fixture\x00secret", "fixture\tsecret"])
def test_secret_rejects_whitespace_and_control_characters_without_echo(google_environment, value):
    google_environment["GOOGLE_CLIENT_SECRET"] = value
    with pytest.raises(AuthSettingsError) as caught:
        GoogleSettings.from_env(google_environment)
    assert "GOOGLE_CLIENT_SECRET" in str(caught.value)
    assert value not in str(caught.value)
    assert "fixture" not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8000/api/auth/google/callback",
        "http://127.0.0.1:5173/api/auth/google/callback",
        "http://127.0.0.1:8000/api/auth/google/callback/",
        "http://127.0.0.1:8000/api/auth/google/callback?next=/",
        "http://127.0.0.1:8000/api/auth/google/callback#fragment",
        "http://127.0.0.1:8000/api/auth/google/callback?",
        "http://user:secret@127.0.0.1:8000/api/auth/google/callback",
        "//127.0.0.1:8000/api/auth/google/callback",
        "https://evil.example.test/api/auth/google/callback",
        "https://127.0.0.1:8000/api/auth/google/callback",
    ],
)
def test_callback_must_match_api_origin_and_exact_path(google_environment, value):
    google_environment["GOOGLE_REDIRECT_URI"] = value
    with pytest.raises(AuthSettingsError) as caught:
        GoogleSettings.from_env(google_environment)
    assert "GOOGLE_REDIRECT_URI" in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    "value", ["private-invalid-base64", *[base64.b64encode(b"x" * n).decode() for n in (0, 16, 31, 33, 64)]]
)
def test_transaction_key_requires_exactly_32_decoded_bytes_without_echo(google_environment, value):
    google_environment["GOOGLE_TRANSACTION_KEY"] = value
    with pytest.raises(AuthSettingsError) as caught:
        GoogleSettings.from_env(google_environment)
    assert "GOOGLE_TRANSACTION_KEY" in str(caught.value)
    if value:
        assert value not in str(caught.value)


@pytest.mark.parametrize("name", AUTH_KEYS)
def test_transaction_key_cannot_reuse_any_auth_key(google_environment, name):
    google_environment["GOOGLE_TRANSACTION_KEY"] = google_environment[name]
    with pytest.raises(AuthSettingsError, match="GOOGLE_TRANSACTION_KEY"):
        GoogleSettings.from_env(google_environment)


def test_settings_repr_hides_secret_and_transaction_key(google_environment):
    settings = GoogleSettings.from_env(google_environment)
    representation = repr(settings)
    assert settings.client_secret not in representation
    assert google_environment["GOOGLE_TRANSACTION_KEY"] not in representation
    assert repr(settings.transaction_key) not in representation


def test_explicit_auth_settings_define_callback_and_key_separation(google_environment):
    auth = AuthSettings.from_env(google_environment)
    google_only = {name: google_environment[name] for name in GOOGLE_NAMES}
    assert GoogleSettings.from_env(google_only, auth_settings=auth) == GoogleSettings.from_env(google_environment)
    google_only["GOOGLE_TRANSACTION_KEY"] = google_environment["AUTH_CSRF_KEY"]
    with pytest.raises(AuthSettingsError, match="GOOGLE_TRANSACTION_KEY"):
        GoogleSettings.from_env(google_only, auth_settings=auth)


def test_google_loader_uses_process_environment(google_environment, monkeypatch):
    for name, value in google_environment.items():
        monkeypatch.setenv(name, value)
    assert get_google_settings() == GoogleSettings.from_env(google_environment)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    with pytest.raises(AuthSettingsError, match="GOOGLE_CLIENT_SECRET"):
        get_google_settings()
