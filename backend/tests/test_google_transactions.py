"""Real AES-GCM, time boundaries, PKCE and fail-closed login transaction checks."""

import base64
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from app.security.google_transactions import (
    GOOGLE_TRANSACTION_TTL_SECONDS,
    GoogleTransactionError,
    InvalidGoogleReturnTo,
    create_google_transaction,
    encode_google_transaction,
    validate_google_return_to,
    verify_google_transaction,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def unbase64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def authenticated_payload(payload, settings, *, context=None):
    """잘못된 데이터를 암호화해 복호화 이후의 내용 검증도 동작하는지 확인한다."""
    nonce = b"fixture-only"
    if context is None:
        context = json.dumps(
            ["google-login-transaction", "v1", settings.client_id, settings.redirect_uri], separators=(",", ":")
        ).encode()
    raw = nonce + AESGCM(settings.transaction_key).encrypt(nonce, json.dumps(payload).encode(), context)
    return "v1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_generated_values_are_independent_cookie_safe_and_hidden():
    first = create_google_transaction(now=NOW)
    second = create_google_transaction(now=NOW)
    values = [
        item
        for transaction in (first, second)
        for item in (transaction.state, transaction.nonce, transaction.code_verifier)
    ]
    assert len(set(values)) == 6
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", value) for value in values)
    assert all(len(unbase64(value)) == 32 for value in values)
    assert first.expires_at - first.issued_at == GOOGLE_TRANSACTION_TTL_SECONDS == 300
    assert first.return_to == "/"
    assert all(value not in repr(first) for value in values)


def test_pkce_matches_rfc7636_s256_known_vector():
    transaction = replace(
        create_google_transaction(now=NOW), code_verifier="dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    assert transaction.code_challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_round_trip_hides_values_and_randomizes_encryption_nonce(google_settings):
    transaction = create_google_transaction(now=NOW)
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    another = encode_google_transaction(transaction, google_settings, now=NOW)
    assert len(cookie) < 2048
    assert cookie != another
    assert unbase64(cookie.split(".")[1])[:12] != unbase64(another.split(".")[1])[:12]
    assert verify_google_transaction(cookie, transaction.state, google_settings, now=NOW) == transaction
    raw = unbase64(cookie.split(".")[1])
    for value in (transaction.state, transaction.nonce, transaction.code_verifier):
        assert value not in cookie
        assert value.encode() not in raw


def test_cookie_validation_alone_does_not_claim_single_use(google_settings):
    # Phase 4·7에서 세션 발급 전에 Google code의 일회 교환을 확인해야 한다.
    transaction = create_google_transaction(now=NOW)
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    for _ in range(2):
        assert verify_google_transaction(cookie, transaction.state, google_settings, now=NOW) == transaction


@pytest.mark.parametrize("seconds", [0, 299, 299.999])
def test_cookie_is_valid_only_before_expiry(google_settings, seconds):
    transaction = create_google_transaction(now=NOW)
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    assert (
        verify_google_transaction(cookie, transaction.state, google_settings, now=NOW + timedelta(seconds=seconds))
        == transaction
    )


@pytest.mark.parametrize("seconds", [-1, -0.001, 300, 301])
def test_future_or_expired_cookie_rejected_at_exact_boundaries(google_settings, seconds):
    transaction = create_google_transaction(now=NOW)
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    with pytest.raises(GoogleTransactionError):
        verify_google_transaction(cookie, transaction.state, google_settings, now=NOW + timedelta(seconds=seconds))


@pytest.mark.parametrize("index", [0, 12, -1])
def test_nonce_ciphertext_or_tag_tampering_is_rejected(google_settings, index):
    transaction = create_google_transaction(now=NOW)
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    raw = bytearray(unbase64(cookie.split(".")[1]))
    raw[index] ^= 1
    changed = "v1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with pytest.raises(GoogleTransactionError):
        verify_google_transaction(changed, transaction.state, google_settings, now=NOW)


@pytest.mark.parametrize("change", ["transaction_key", "client_id", "redirect_uri"])
def test_cookie_is_bound_to_key_client_and_callback(google_settings, change):
    transaction = create_google_transaction(now=NOW)
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    changed = b"x" * 32 if change == "transaction_key" else "fixture-other-context"
    settings = replace(google_settings, **{change: changed})
    with pytest.raises(GoogleTransactionError):
        verify_google_transaction(cookie, transaction.state, settings, now=NOW)


@pytest.mark.parametrize(
    "value", [None, "", "v2.abc", "v1.abc=", "v1.abc!", "v1.a", "v1.YQ", "v1." + "x" * 2048, "v1.☃"]
)
def test_missing_malformed_and_oversized_cookie_fails_safely(google_settings, value):
    with pytest.raises(GoogleTransactionError) as caught:
        verify_google_transaction(value, "a" * 43, google_settings, now=NOW)
    if value:
        assert value not in str(caught.value)


@pytest.mark.parametrize("state", [None, "", "wrong", "x" * 43, "☃" * 43, "x" * 10000])
def test_state_missing_mismatch_or_invalid_format_is_rejected(google_settings, state):
    transaction = create_google_transaction(now=NOW)
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    with pytest.raises(GoogleTransactionError) as caught:
        verify_google_transaction(cookie, state, google_settings, now=NOW)
    assert cookie not in str(caught.value)
    assert transaction.state not in str(caught.value)


@pytest.mark.parametrize(
    "value", ["", "//evil.test", "https://evil.test", "/chat", "/?next=evil", "/#evil", "/\\evil", "/%2fevil", "/\n"]
)
def test_return_to_requires_exact_allowlist_membership(value):
    with pytest.raises(InvalidGoogleReturnTo) as caught:
        create_google_transaction(value, now=NOW)
    if value:
        assert value not in str(caught.value)
    assert validate_google_return_to(None) == validate_google_return_to("/") == "/"


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "short"),
        ("state", None),
        ("nonce", "☃" * 43),
        ("code_verifier", "short"),
        ("return_to", "//evil.test"),
        ("return_to", ["/"]),
        ("issued_at", True),
        ("issued_at", "123"),
        ("expires_at", False),
        ("expires_at", 9999999999),
    ],
)
def test_authenticated_but_invalid_payload_is_rejected(google_settings, field, value):
    transaction = create_google_transaction(now=NOW)
    payload = {**asdict(transaction), field: value}
    cookie = authenticated_payload(payload, google_settings)
    with pytest.raises(GoogleTransactionError):
        verify_google_transaction(cookie, transaction.state, google_settings, now=NOW)


@pytest.mark.parametrize("kind", ["missing", "extra", "not-object", "wrong-purpose"])
def test_invalid_schema_or_other_encryption_purpose_is_rejected(google_settings, kind):
    transaction = create_google_transaction(now=NOW)
    payload = asdict(transaction)
    if kind == "missing":
        del payload["nonce"]
    elif kind == "extra":
        payload["extra"] = "fixture"
    elif kind == "not-object":
        payload = [payload]
    cookie = authenticated_payload(
        payload, google_settings, context=b"other-purpose" if kind == "wrong-purpose" else None
    )
    with pytest.raises(GoogleTransactionError):
        verify_google_transaction(cookie, transaction.state, google_settings, now=NOW)


@pytest.mark.parametrize("length", [16, 24, 31, 33])
def test_encoder_requires_aes256_key_even_for_direct_dataclass_construction(google_settings, length):
    transaction = create_google_transaction(now=NOW)
    with pytest.raises(GoogleTransactionError):
        encode_google_transaction(transaction, replace(google_settings, transaction_key=b"x" * length), now=NOW)


def test_naive_time_rejected_and_non_utc_time_supported(google_settings):
    with pytest.raises(GoogleTransactionError):
        create_google_transaction(now=NOW.replace(tzinfo=None))
    transaction = create_google_transaction(now=NOW.astimezone(timezone(timedelta(hours=9))))
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    assert verify_google_transaction(cookie, transaction.state, google_settings, now=NOW) == transaction
    with pytest.raises(GoogleTransactionError):
        verify_google_transaction(cookie, transaction.state, google_settings, now=NOW.replace(tzinfo=None))


def test_default_clock_and_microsecond_precision(google_settings):
    transaction = create_google_transaction()
    cookie = encode_google_transaction(transaction, google_settings)
    assert verify_google_transaction(cookie, transaction.state, google_settings) == transaction
    fractional = create_google_transaction(now=NOW + timedelta(microseconds=500000))
    assert fractional.issued_at == int(NOW.timestamp())
