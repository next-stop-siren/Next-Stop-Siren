"""Verify access JWT policy using fake keys and a deterministic clock."""

import base64
from datetime import datetime, timedelta, timezone
import json
import hmac
from unittest.mock import patch

import jwt
import pytest

from app.security.tokens import TokenVerifyError, issue_access_token, issue_refresh_token, verify_access_token

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
TIMESTAMP = int(NOW.timestamp())
JTI = "123e4567-e89b-42d3-a456-426614174000"
REQUIRED = ["iss", "aud", "sub", "iat", "nbf", "exp", "jti", "token_type"]


@pytest.fixture(autouse=True)
def fixed_clock():
    # 기다리는 대신 검증 라이브러리의 시계를 고정해 만료 경계를 정확히 검사한다.
    class FixedDatetime(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz is not None else cls.current.replace(tzinfo=None)

    with patch("jwt.api_jwt.datetime", FixedDatetime):
        yield FixedDatetime


@pytest.fixture
def claims(auth_settings):
    return {
        "iss": auth_settings.issuer,
        "aud": auth_settings.audience,
        "sub": "101",
        "iat": TIMESTAMP,
        "nbf": TIMESTAMP,
        "exp": TIMESTAMP + 900,
        "jti": JTI,
        "token_type": "access",
    }


@pytest.fixture
def sign(auth_settings):
    def encode(claims, *, headers=None, key=None, algorithm="HS256"):
        # 발급 라이브러리가 거부하는 헤더·claim도 검증 함수에 도달하도록 직접 서명한다.
        header = {"alg": algorithm, "typ": "JWT", "kid": auth_settings.access_kid, **(headers or {})}
        parts = [base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=") for value in (header, claims)]
        message = b".".join(parts)
        digest = {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}[algorithm]
        signing_key = auth_settings.access_key if key is None else key
        signature = base64.urlsafe_b64encode(hmac.digest(signing_key, message, digest)).rstrip(b"=")
        return (message + b"." + signature).decode()

    return encode


@pytest.mark.parametrize("user_id", [1, 101, 9007199254740993, 9223372036854775807])
def test_server_issued_access_returns_exact_integer_id(auth_settings, user_id: int) -> None:
    token = issue_access_token(user_id, auth_settings, now=NOW)
    result = verify_access_token(token.token, auth_settings)
    assert type(result) is int and result == user_id


@pytest.mark.parametrize("lifetime", [1, 900])
def test_valid_token_within_fifteen_minutes_is_accepted(auth_settings, claims, sign, lifetime: int) -> None:
    claims["exp"] = TIMESTAMP + lifetime
    assert verify_access_token(sign(claims), auth_settings) == 101


@pytest.mark.parametrize("claim", REQUIRED)
@pytest.mark.parametrize("missing", [True, False], ids=["absent", "null"])
def test_all_claims_are_required_even_with_valid_signature(
    auth_settings, claims, sign, claim: str, missing: bool
) -> None:
    if missing:
        del claims[claim]
    else:
        claims[claim] = None
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims), auth_settings)


@pytest.mark.parametrize(
    "claim,value",
    [
        ("iss", "other-issuer"),
        ("iss", 123),
        ("iss", []),
        ("aud", "other-audience"),
        ("aud", ["fixture-api"]),
        ("aud", 123),
        ("aud", []),
        ("token_type", "refresh"),
        ("token_type", "Access"),
        ("token_type", True),
        ("token_type", []),
    ],
)
def test_claim_identity_and_kind_must_match(auth_settings, claims, sign, claim: str, value) -> None:
    claims[claim] = value
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims), auth_settings)


@pytest.mark.parametrize(
    "subject",
    [
        101,
        True,
        "",
        "0",
        "-1",
        "+1",
        "01",
        "1.0",
        "1e2",
        " 101",
        "101 ",
        "١٠١",
        "１２３",
        "9223372036854775808",
        "9" * 5000,
    ],
    ids=[
        "number",
        "bool",
        "empty",
        "zero",
        "negative",
        "plus",
        "leading-zero",
        "float",
        "exponent",
        "leading-space",
        "trailing-space",
        "arabic-digits",
        "fullwidth-digits",
        "overflow",
        "oversized",
    ],
)
def test_subject_must_be_canonical_positive_bigint(auth_settings, claims, sign, subject) -> None:
    claims["sub"] = subject
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims), auth_settings)


@pytest.mark.parametrize("claim", ["iat", "nbf", "exp"])
@pytest.mark.parametrize("value", [True, False, {}, [], float("nan"), float("inf"), -1])
def test_timestamp_claims_reject_invalid_types_and_negative_values(
    auth_settings, claims, sign, claim: str, value
) -> None:
    claims[claim] = value
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims), auth_settings)


@pytest.mark.parametrize("claim", ["iat", "nbf", "exp"])
@pytest.mark.parametrize("conversion", [str, float], ids=["numeric-string", "float"])
def test_convertible_timestamps_are_still_rejected(auth_settings, claims, sign, claim: str, conversion) -> None:
    claims[claim] = conversion(claims[claim])
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims), auth_settings)


@pytest.mark.parametrize(
    "changes",
    [
        {"iat": TIMESTAMP + 1},
        {"nbf": TIMESTAMP + 1},
        {"iat": TIMESTAMP - 900, "nbf": TIMESTAMP - 900, "exp": TIMESTAMP},
        {"nbf": TIMESTAMP - 1},
        {"exp": TIMESTAMP + 901},
        {"iat": TIMESTAMP - 1000, "nbf": TIMESTAMP - 1000},
    ],
    ids=[
        "future-iat",
        "future-nbf",
        "expired",
        "nbf-before-iat",
        "lifetime-too-long",
        "old-issuance-with-future-expiry",
    ],
)
def test_temporal_policy_is_enforced(auth_settings, claims, sign, changes) -> None:
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign({**claims, **changes}), auth_settings)


def test_exact_expiry_is_rejected_but_one_microsecond_before_is_accepted(auth_settings, fixed_clock) -> None:
    token = issue_access_token(101, auth_settings, now=NOW - timedelta(seconds=900))
    fixed_clock.current = NOW - timedelta(microseconds=1)
    assert verify_access_token(token.token, auth_settings) == 101
    fixed_clock.current = NOW
    with pytest.raises(TokenVerifyError):
        verify_access_token(token.token, auth_settings)


def test_exact_not_before_is_accepted_but_early_request_is_rejected(auth_settings, fixed_clock) -> None:
    token = issue_access_token(101, auth_settings, now=NOW)
    fixed_clock.current = NOW - timedelta(microseconds=1)
    with pytest.raises(TokenVerifyError):
        verify_access_token(token.token, auth_settings)
    fixed_clock.current = NOW
    assert verify_access_token(token.token, auth_settings) == 101


@pytest.mark.parametrize(
    "jti",
    [
        "",
        " ",
        "not-a-uuid",
        123,
        True,
        [],
        "00000000-0000-0000-0000-000000000000",
        "123e4567-e89b-12d3-a456-426614174000",
        JTI.upper(),
        JTI.replace("-", ""),
        "{" + JTI + "}",
    ],
)
def test_jti_must_match_issued_uuid_v4_format(auth_settings, claims, sign, jti) -> None:
    claims["jti"] = jti
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims), auth_settings)


@pytest.mark.parametrize(
    "headers",
    [
        {"kid": None},
        {"kid": "unknown-key"},
        {"kid": "test-refresh-v1"},
        {"kid": []},
        {"typ": None},
        {"typ": "refresh"},
        {"crit": ["custom"]},
        {"crit": []},
        {"b64": True},
    ],
)
def test_header_policy_rejects_unknown_keys_types_and_extensions(auth_settings, claims, sign, headers) -> None:
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims, headers=headers), auth_settings)


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_token_cannot_choose_a_different_algorithm(auth_settings, claims, sign, algorithm: str) -> None:
    token = sign(claims, algorithm=algorithm, key=auth_settings.access_key * 2)
    with pytest.raises(TokenVerifyError):
        verify_access_token(token, auth_settings)


def test_unsigned_token_is_rejected(auth_settings, claims) -> None:
    token = jwt.encode(claims, None, algorithm="none", headers={"kid": auth_settings.access_kid})
    with pytest.raises(TokenVerifyError):
        verify_access_token(token, auth_settings)


def test_wrong_signing_key_is_rejected(auth_settings, claims, sign) -> None:
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims, key=b"x" * 32), auth_settings)


def test_payload_tampering_cannot_replace_user_id(auth_settings) -> None:
    token = issue_access_token(101, auth_settings, now=NOW)
    header, payload, signature = token.token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "202"
    altered = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    with pytest.raises(TokenVerifyError):
        verify_access_token(f"{header}.{altered}.{signature}", auth_settings)


def test_refresh_token_is_rejected_even_when_resigned_with_access_key(auth_settings, claims, sign) -> None:
    refresh = issue_refresh_token(101, auth_settings, now=NOW)
    with pytest.raises(TokenVerifyError):
        verify_access_token(refresh.token, auth_settings)
    claims["token_type"] = "refresh"
    claims["exp"] = TIMESTAMP + 900
    with pytest.raises(TokenVerifyError):
        verify_access_token(sign(claims), auth_settings)


@pytest.mark.parametrize(
    "token",
    [None, b"fixture-token", 123, True, "", "not-a-jwt", "a.b.c", "a.b", "a.b.c.d", "Bearer fixture-token", "☃.☃.☃"],
)
def test_malformed_input_has_a_safe_common_error(auth_settings, token) -> None:
    with pytest.raises(TokenVerifyError) as caught:
        verify_access_token(token, auth_settings)
    assert str(caught.value) == "유효한 access 토큰이 필요합니다."


def test_validation_failure_does_not_echo_tokens_keys_or_library_details(auth_settings) -> None:
    secret = "private-token-value"
    with patch("app.security.tokens.jwt.decode_complete", side_effect=jwt.DecodeError("private library details")):
        with pytest.raises(TokenVerifyError) as caught:
            verify_access_token(secret, auth_settings)
    assert secret not in str(caught.value) and "private" not in str(caught.value)
    assert repr(auth_settings.access_key) not in str(caught.value)
    assert caught.value.__cause__ is None


def test_unexpected_programming_errors_are_not_hidden(auth_settings) -> None:
    with patch("app.security.tokens.jwt.decode_complete", side_effect=RuntimeError("programming failure")):
        with pytest.raises(RuntimeError, match="programming failure"):
            verify_access_token("fixture-token", auth_settings)
