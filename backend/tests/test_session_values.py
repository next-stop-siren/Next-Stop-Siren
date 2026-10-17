"""Refresh digest and CSRF binding checks using only fake session data."""

import base64
from dataclasses import replace
import hashlib
import hmac

import pytest

from app.security.session_values import create_csrf_token, create_refresh_digest, verify_csrf_token
from app.security.settings import AuthSettings
from app.security.tokens import issue_refresh_token


def test_digest_is_keyed_sha256_of_exact_refresh_jwt(auth_settings: AuthSettings) -> None:
    refresh = issue_refresh_token(101, auth_settings)
    digest = create_refresh_digest(refresh.token, auth_settings)
    assert isinstance(digest, bytes)
    assert len(digest) == 32
    assert digest == hmac.digest(auth_settings.refresh_digest_key, refresh.token.encode(), "sha256")
    assert digest != hashlib.sha256(refresh.token.encode()).digest()
    assert digest != hmac.digest(auth_settings.refresh_key, refresh.token.encode(), "sha256")


def test_digest_changes_when_refresh_or_digest_key_changes(auth_settings: AuthSettings) -> None:
    first = issue_refresh_token(101, auth_settings)
    second = issue_refresh_token(101, auth_settings)
    digest = create_refresh_digest(first.token, auth_settings)
    assert digest == create_refresh_digest(first.token, auth_settings)
    assert digest != create_refresh_digest(second.token, auth_settings)
    changed = replace(auth_settings, refresh_digest_key=b"fixture-different-digest-key-32bytes")
    assert digest != create_refresh_digest(first.token, changed)


def test_csrf_is_cookie_safe_mac_bound_to_current_jti(auth_settings: AuthSettings) -> None:
    refresh = issue_refresh_token(101, auth_settings)
    csrf = create_csrf_token(refresh.jti, auth_settings)
    raw_mac = base64.urlsafe_b64decode(csrf + "=" * (-len(csrf) % 4))
    assert len(raw_mac) == 32
    assert raw_mac == hmac.digest(auth_settings.csrf_key, b"csrf:v1:" + refresh.jti.encode(), "sha256")
    assert len(csrf) == 43
    assert all(character.isalnum() or character in "-_" for character in csrf)
    assert refresh.jti not in csrf
    assert refresh.token not in csrf


def test_csrf_changes_when_refresh_jti_or_csrf_key_changes(auth_settings: AuthSettings) -> None:
    first = issue_refresh_token(101, auth_settings)
    second = issue_refresh_token(101, auth_settings)
    csrf = create_csrf_token(first.jti, auth_settings)
    assert csrf == create_csrf_token(first.jti, auth_settings)
    assert csrf != create_csrf_token(second.jti, auth_settings)
    changed = replace(auth_settings, csrf_key=b"fixture-different-csrf-key-32bytes")
    assert csrf != create_csrf_token(first.jti, changed)


def test_digest_and_csrf_use_separate_keys(auth_settings: AuthSettings) -> None:
    refresh = issue_refresh_token(101, auth_settings)
    digest = create_refresh_digest(refresh.token, auth_settings)
    csrf = create_csrf_token(refresh.jti, auth_settings)
    changed_csrf = replace(auth_settings, csrf_key=b"fixture-different-csrf-key-32bytes")
    assert create_refresh_digest(refresh.token, changed_csrf) == digest
    assert create_csrf_token(refresh.jti, changed_csrf) != csrf
    changed_digest = replace(auth_settings, refresh_digest_key=b"fixture-different-digest-key-32bytes")
    assert create_refresh_digest(refresh.token, changed_digest) != digest
    assert create_csrf_token(refresh.jti, changed_digest) == csrf


@pytest.mark.parametrize("value", [None, "", "invalid", "x" * 43, "한" * 43, "x" * 10000])
@pytest.mark.parametrize("field", ["cookie", "header"])
def test_csrf_verification_safely_rejects_missing_or_invalid_values(auth_settings, value, field):
    refresh = issue_refresh_token(101, auth_settings)
    csrf = create_csrf_token(refresh.jti, auth_settings)
    cookie, header = (value, csrf) if field == "cookie" else (csrf, value)
    assert not verify_csrf_token(refresh.jti, cookie, header, auth_settings)


def test_csrf_verification_requires_current_jti_and_csrf_key(auth_settings):
    refresh = issue_refresh_token(101, auth_settings)
    csrf = create_csrf_token(refresh.jti, auth_settings)
    assert verify_csrf_token(refresh.jti, csrf, csrf, auth_settings)
    other = issue_refresh_token(101, auth_settings)
    assert not verify_csrf_token(other.jti, csrf, csrf, auth_settings)
    changed = replace(auth_settings, csrf_key=b"fixture-different-csrf-key-32bytes")
    assert not verify_csrf_token(refresh.jti, csrf, csrf, changed)
