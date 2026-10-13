"""Verify issued JWTs with fake keys, without HTTP or database access."""

import base64
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
from uuid import UUID

import jwt
import pytest

from app.security.settings import AuthSettings
from app.security.tokens import IssuedToken, TokenIssueError, issue_access_token, issue_refresh_token

REQUIRED_CLAIMS = ["iss", "aud", "sub", "iat", "nbf", "exp", "jti", "token_type"]


def decode_issued(token: IssuedToken, settings: AuthSettings) -> dict:
    key = settings.access_key if token.token_type == "access" else settings.refresh_key
    return jwt.decode(
        token.token,
        key,
        algorithms=["HS256"],
        issuer=settings.issuer,
        audience=settings.audience,
        options={"require": REQUIRED_CLAIMS, "strict_aud": True},
    )


@pytest.mark.parametrize("kind,ttl", [("access", 900), ("refresh", 1209600)])
def test_issued_tokens_match_the_complete_contract(auth_settings: AuthSettings, kind: str, ttl: int) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=123456)
    issue = issue_access_token if kind == "access" else issue_refresh_token
    token = issue(9007199254740993, auth_settings, now=now)
    claims = decode_issued(token, auth_settings)
    assert claims == {
        "iss": auth_settings.issuer,
        "aud": auth_settings.audience,
        "sub": "9007199254740993",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(now.timestamp()) + ttl,
        "jti": token.jti,
        "token_type": kind,
    }
    assert UUID(token.jti).version == 4
    assert jwt.get_unverified_header(token.token) == {
        "alg": "HS256",
        "typ": "JWT",
        "kid": auth_settings.access_kid if kind == "access" else auth_settings.refresh_kid,
    }
    assert token.issued_at == now.replace(microsecond=0)
    assert token.expires_at == token.issued_at + timedelta(seconds=ttl)
    assert token.token not in repr(token)


def test_each_issuance_gets_an_independent_jti(auth_settings: AuthSettings) -> None:
    now = datetime.now(timezone.utc)
    tokens = [issue_access_token(101, auth_settings, now=now) for _ in range(2)]
    tokens += [issue_refresh_token(101, auth_settings, now=now) for _ in range(2)]
    assert len({token.jti for token in tokens}) == 4
    assert len({token.token for token in tokens}) == 4


def test_default_clock_and_non_utc_clock_are_supported(auth_settings: AuthSettings) -> None:
    before = int(datetime.now(timezone.utc).timestamp())
    default = issue_access_token(101, auth_settings)
    after = int(datetime.now(timezone.utc).timestamp())
    assert before <= decode_issued(default, auth_settings)["iat"] <= after
    now = datetime.now(timezone(timedelta(hours=9)))
    converted = issue_refresh_token(101, auth_settings, now=now)
    assert converted.issued_at.tzinfo == timezone.utc
    assert decode_issued(converted, auth_settings)["iat"] == int(now.timestamp())


@pytest.mark.parametrize("kind", ["access", "refresh"])
def test_other_token_key_cannot_verify_the_signature(auth_settings: AuthSettings, kind: str) -> None:
    token = issue_access_token(101, auth_settings) if kind == "access" else issue_refresh_token(101, auth_settings)
    wrong_key = auth_settings.refresh_key if kind == "access" else auth_settings.access_key
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token.token, wrong_key, algorithms=["HS256"], audience=auth_settings.audience)


def test_modified_user_claim_invalidates_signature(auth_settings: AuthSettings) -> None:
    token = issue_access_token(101, auth_settings)
    header, payload, signature = token.token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "202"
    altered = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            f"{header}.{altered}.{signature}",
            auth_settings.access_key,
            algorithms=["HS256"],
            audience=auth_settings.audience,
        )


@pytest.mark.parametrize("issue,ttl", [(issue_access_token, 900), (issue_refresh_token, 1209600)])
def test_expiry_boundary_is_rejected(auth_settings: AuthSettings, issue, ttl: int) -> None:
    token = issue(101, auth_settings, now=datetime.now(timezone.utc) - timedelta(seconds=ttl))
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_issued(token, auth_settings)


def test_future_issuance_is_not_yet_valid(auth_settings: AuthSettings) -> None:
    token = issue_access_token(101, auth_settings, now=datetime.now(timezone.utc) + timedelta(minutes=5))
    with pytest.raises(jwt.ImmatureSignatureError):
        decode_issued(token, auth_settings)


@pytest.mark.parametrize("user_id", [True, 0, -1, 1.5, "101", 9223372036854775808])
def test_invalid_internal_user_ids_are_rejected(auth_settings: AuthSettings, user_id) -> None:
    with pytest.raises(TokenIssueError):
        issue_access_token(user_id, auth_settings)


def test_naive_issuance_time_is_rejected(auth_settings: AuthSettings) -> None:
    with pytest.raises(TokenIssueError):
        issue_refresh_token(101, auth_settings, now=datetime(2026, 1, 2))


def test_signing_failures_do_not_expose_keys_or_internal_details(auth_settings: AuthSettings) -> None:
    with patch("app.security.tokens.jwt.encode", side_effect=jwt.InvalidKeyError("private key details")):
        with pytest.raises(TokenIssueError) as caught:
            issue_access_token(101, auth_settings)
    assert "private" not in str(caught.value)
    assert repr(auth_settings.access_key) not in str(caught.value)


def test_programming_errors_are_not_hidden(auth_settings: AuthSettings) -> None:
    with patch("app.security.tokens.jwt.encode", side_effect=RuntimeError("programming failure")):
        with pytest.raises(RuntimeError, match="programming failure"):
            issue_refresh_token(101, auth_settings)
