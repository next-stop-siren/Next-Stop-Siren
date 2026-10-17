"""Refresh-specific verification and absolute expiry, with fake keys and a fixed clock."""

import base64
from datetime import datetime, timedelta, timezone
import json
import hmac
from unittest.mock import patch

import jwt
import pytest

from app.security.tokens import (
    TokenIssueError,
    TokenVerifyError,
    issue_access_token,
    issue_refresh_token,
    verify_access_token,
    verify_refresh_token,
)

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)
TTL = 14 * 24 * 60 * 60


@pytest.fixture(autouse=True)
def fixed_clock():
    class FixedDatetime(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz is not None else cls.current.replace(tzinfo=None)

    with patch("jwt.api_jwt.datetime", FixedDatetime):
        yield FixedDatetime


@pytest.fixture
def claims(auth_settings):
    issued = issue_refresh_token(101, auth_settings, now=NOW)
    return jwt.decode(issued.token, auth_settings.refresh_key, algorithms=["HS256"], audience=auth_settings.audience)


@pytest.fixture
def sign(auth_settings):
    def encode(claims, *, headers=None, key=None, algorithm="HS256"):
        # 발급기가 거부하는 잘못된 claim도 검증 함수에 전달할 수 있게 직접 서명한다.
        header = {"alg": algorithm, "typ": "JWT", "kid": auth_settings.refresh_kid, **(headers or {})}
        parts = [base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=") for value in (header, claims)]
        message = b".".join(parts)
        digest = {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}[algorithm]
        signing_key = auth_settings.refresh_key if key is None else key
        signature = base64.urlsafe_b64encode(hmac.digest(signing_key, message, digest)).rstrip(b"=")
        return (message + b"." + signature).decode()

    return encode


@pytest.mark.parametrize("user_id", [1, 101, 9007199254740993, 9223372036854775807])
def test_refresh_returns_verified_metadata_without_raw_token(auth_settings, user_id):
    issued = issue_refresh_token(user_id, auth_settings, now=NOW)
    verified = verify_refresh_token(issued.token, auth_settings)
    assert type(verified.user_id) is int and verified.user_id == user_id
    assert verified.jti == issued.jti
    assert verified.issued_at == issued.issued_at == NOW
    assert verified.expires_at == issued.expires_at == NOW + timedelta(seconds=TTL)
    assert issued.token not in repr(verified)


def test_multiple_rotations_keep_root_expiry_and_change_jti(auth_settings, fixed_clock):
    root = issue_refresh_token(101, auth_settings, now=NOW)
    tokens = [root]
    for elapsed in (1, 7 * 86400, TTL - 1):
        fixed_clock.current = NOW + timedelta(seconds=elapsed)
        rotated = issue_refresh_token(101, auth_settings, now=fixed_clock.current, absolute_expires_at=root.expires_at)
        verified = verify_refresh_token(rotated.token, auth_settings)
        assert verified.expires_at == root.expires_at
        assert verified.issued_at == fixed_clock.current
        tokens.append(rotated)
    assert len({token.jti for token in tokens}) == 4
    fixed_clock.current = root.expires_at
    for token in tokens:
        with pytest.raises(TokenVerifyError):
            verify_refresh_token(token.token, auth_settings)
    with pytest.raises(TokenIssueError):
        issue_refresh_token(101, auth_settings, now=root.expires_at, absolute_expires_at=root.expires_at)


@pytest.mark.parametrize("offset", [-1, 0, TTL + 1])
def test_expired_or_extended_absolute_expiry_cannot_be_issued(auth_settings, offset):
    with pytest.raises(TokenIssueError):
        issue_refresh_token(101, auth_settings, now=NOW, absolute_expires_at=NOW + timedelta(seconds=offset))


def test_absolute_expiry_requires_timezone_and_normalizes_to_utc_seconds(auth_settings):
    with pytest.raises(TokenIssueError):
        issue_refresh_token(101, auth_settings, now=NOW, absolute_expires_at=datetime(2026, 10, 18))
    expiry = (NOW + timedelta(days=1, microseconds=123456)).astimezone(timezone(timedelta(hours=9)))
    token = issue_refresh_token(101, auth_settings, now=NOW, absolute_expires_at=expiry)
    assert token.expires_at == NOW + timedelta(days=1)
    assert verify_refresh_token(token.token, auth_settings).expires_at == token.expires_at


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "iat", "nbf", "exp", "jti", "token_type"])
@pytest.mark.parametrize("missing", [True, False], ids=["absent", "null"])
def test_refresh_requires_all_claims(auth_settings, claims, sign, claim, missing):
    if missing:
        del claims[claim]
    else:
        claims[claim] = None
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(sign(claims), auth_settings)


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "other"},
        {"aud": "other"},
        {"aud": ["fixture-api"]},
        {"sub": "01"},
        {"sub": "9223372036854775808"},
        {"jti": "not-a-uuid"},
        {"token_type": "access"},
        {"iat": int(NOW.timestamp()) + 1},
        {"nbf": int(NOW.timestamp()) + 1},
        {"nbf": int(NOW.timestamp()) - 1},
        {"iat": str(int(NOW.timestamp()))},
        {"nbf": float(NOW.timestamp())},
        {"exp": True},
        {"exp": int(NOW.timestamp())},
        {"exp": int(NOW.timestamp()) + TTL + 1},
    ],
)
def test_refresh_rejects_wrong_identity_types_and_times(auth_settings, claims, sign, changes):
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(sign({**claims, **changes}), auth_settings)


@pytest.mark.parametrize("headers", [{"kid": "unknown"}, {"kid": "test-access-v1"}, {"typ": "other"}, {"crit": []}])
def test_refresh_header_policy(auth_settings, claims, sign, headers):
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(sign(claims, headers=headers), auth_settings)


@pytest.mark.parametrize("algorithm", ["HS384", "HS512", "none"])
def test_refresh_cannot_choose_another_algorithm(auth_settings, claims, sign, algorithm):
    token = (
        jwt.encode(claims, None, algorithm="none")
        if algorithm == "none"
        else sign(claims, algorithm=algorithm, key=b"x" * 64)
    )
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(token, auth_settings)


def test_access_and_refresh_cannot_be_interchanged_even_with_other_kind_key(auth_settings, claims, sign):
    access = issue_access_token(101, auth_settings, now=NOW)
    refresh = issue_refresh_token(101, auth_settings, now=NOW)
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(access.token, auth_settings)
    with pytest.raises(TokenVerifyError):
        verify_access_token(refresh.token, auth_settings)
    # 올바른 refresh 키로 서명해도 access 종류의 claim은 거부한다.
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(sign({**claims, "token_type": "access"}), auth_settings)
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(sign(claims, key=auth_settings.access_key), auth_settings)


def test_refresh_expiry_and_not_before_boundaries(auth_settings, fixed_clock):
    token = issue_refresh_token(101, auth_settings, now=NOW)
    fixed_clock.current = NOW - timedelta(microseconds=1)
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(token.token, auth_settings)
    fixed_clock.current = token.expires_at - timedelta(microseconds=1)
    assert verify_refresh_token(token.token, auth_settings).user_id == 101
    fixed_clock.current = token.expires_at
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(token.token, auth_settings)


@pytest.mark.parametrize("token", [None, b"secret", 123, True, "", "not-a-jwt", "a.b.c"])
def test_refresh_invalid_input_has_safe_error(auth_settings, token):
    with pytest.raises(TokenVerifyError, match="유효한 refresh 토큰이 필요합니다."):
        verify_refresh_token(token, auth_settings)


def test_tampered_payload_is_rejected(auth_settings):
    token = issue_refresh_token(101, auth_settings, now=NOW).token
    header, _, signature = token.split(".")
    altered = base64.urlsafe_b64encode(json.dumps({"sub": "202"}).encode()).decode().rstrip("=")
    with pytest.raises(TokenVerifyError):
        verify_refresh_token(f"{header}.{altered}.{signature}", auth_settings)


def test_refresh_errors_do_not_expose_library_details(auth_settings):
    with patch("app.security.tokens.jwt.decode_complete", side_effect=jwt.DecodeError("private library details")):
        with pytest.raises(TokenVerifyError) as caught:
            verify_refresh_token("private-token", auth_settings)
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
