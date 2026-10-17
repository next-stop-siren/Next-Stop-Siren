"""Real RSA signature and Google OIDC claim validation with fake identities."""

import base64
from datetime import timedelta, timezone
import hashlib

from joserfc.jwk import KeySet
import jwt
import pytest

from app.security.google_identity import GoogleIdentityError, verify_google_id_token
from app.security.google_transactions import create_google_transaction
from google_helpers import ACCESS_TOKEN, KID, NOW, google_claims, public_jwks, signed_token


@pytest.fixture
def material(google_settings, google_rsa_private_key):
    transaction = create_google_transaction(now=NOW)
    claims = google_claims(transaction, google_settings)
    keys = KeySet.import_key_set(public_jwks(google_rsa_private_key))

    def verify(values=None, *, header=None, keyset=None, now=NOW):
        token = signed_token(claims if values is None else values, google_rsa_private_key, header=header)
        return verify_google_id_token(
            token, ACCESS_TOKEN, keys if keyset is None else keyset, transaction, google_settings, now=now
        )

    return transaction, claims, verify


@pytest.mark.parametrize("issuer", ["https://accounts.google.com", "accounts.google.com"])
def test_valid_google_identity_uses_canonical_issuer_and_hides_personal_data(material, issuer):
    _, claims, verify = material
    identity = verify({**claims, "iss": issuer})
    assert identity.issuer == "https://accounts.google.com"
    assert identity.subject == claims["sub"] and identity.email == claims["email"]
    assert identity.subject not in repr(identity) and identity.email not in repr(identity)


@pytest.mark.parametrize("claim", ["iss", "sub", "aud", "iat", "exp", "nonce", "email", "email_verified"])
def test_every_required_claim_is_enforced(material, claim):
    _, claims, verify = material
    del claims[claim]
    with pytest.raises(GoogleIdentityError):
        verify()


@pytest.mark.parametrize(
    "name,value",
    [
        ("iss", "https://evil.test"),
        ("iss", None),
        ("iss", ["https://accounts.google.com"]),
        ("sub", None),
        ("sub", 123),
        ("sub", ""),
        ("sub", "x" * 256),
        ("sub", "a b"),
        ("sub", "☃"),
        ("aud", "other-client"),
        ("aud", []),
        ("aud", [123]),
        ("aud", {}),
        ("iat", True),
        ("iat", "123"),
        ("iat", 1.5),
        ("iat", -1),
        ("iat", int(NOW.timestamp()) + 1),
        ("exp", True),
        ("exp", "123"),
        ("exp", 1.5),
        ("exp", int(NOW.timestamp())),
        ("nonce", "wrong"),
        ("nonce", "☃"),
        ("nonce", None),
        ("email", None),
        ("email", ""),
        ("email", "not-email"),
        ("email", " x@example.test"),
        ("email", "x@example.test\n"),
        ("email", "x" * 310 + "@example.test"),
        ("email_verified", False),
        ("email_verified", "true"),
        ("email_verified", 1),
        ("email_verified", None),
        ("nbf", True),
        ("nbf", "123"),
        ("nbf", int(NOW.timestamp()) + 1),
        ("azp", "other-client"),
    ],
)
def test_claim_value_type_and_time_failures_are_rejected(material, name, value):
    _, claims, verify = material
    with pytest.raises(GoogleIdentityError):
        verify({**claims, name: value})


def test_nonce_supported_false_cannot_disable_nonce_verification(material):
    _, claims, verify = material
    with pytest.raises(GoogleIdentityError):
        verify({**claims, "nonce": "wrong", "nonce_supported": False})


def test_multiple_audiences_require_matching_authorized_party(material, google_settings):
    _, claims, verify = material
    values = {**claims, "aud": [google_settings.client_id, "another-client"]}
    with pytest.raises(GoogleIdentityError):
        verify(values)
    assert verify({**values, "azp": google_settings.client_id}).subject == claims["sub"]
    with pytest.raises(GoogleIdentityError):
        verify({**values, "azp": "another-client"})
    with pytest.raises(GoogleIdentityError):
        verify({**values, "aud": [google_settings.client_id, google_settings.client_id]})


def test_optional_access_token_hash_is_verified(material):
    _, claims, verify = material
    digest = hashlib.sha256(ACCESS_TOKEN.encode()).digest()[:16]
    at_hash = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert verify({**claims, "at_hash": at_hash}).subject == claims["sub"]
    with pytest.raises(GoogleIdentityError):
        verify({**claims, "at_hash": "wrong"})


@pytest.mark.parametrize(
    "header",
    [
        {"kid": "unknown-key"},
        {"kid": None},
        {"kid": ""},
        {"typ": "other"},
        {"jku": "https://evil.test/keys"},
        {"jwk": {"kty": "RSA"}},
        {"x5u": "https://evil.test/cert"},
        {"crit": ["evil"], "evil": True},
    ],
)
def test_unapproved_headers_and_unknown_key_id_are_rejected(material, header):
    _, _, verify = material
    with pytest.raises(GoogleIdentityError):
        verify(header=header)


@pytest.mark.parametrize("algorithm", ["none", "HS256", "RS384"])
def test_unsigned_and_unapproved_algorithms_are_rejected(material, google_settings, google_rsa_private_key, algorithm):
    transaction, claims, _ = material
    key = "" if algorithm == "none" else b"fixture-hmac-secret" * 3 if algorithm == "HS256" else google_rsa_private_key
    token = jwt.encode(claims, key, algorithm=algorithm, headers={"kid": KID})
    with pytest.raises(GoogleIdentityError):
        verify_google_id_token(
            token,
            ACCESS_TOKEN,
            KeySet.import_key_set(public_jwks(google_rsa_private_key)),
            transaction,
            google_settings,
            now=NOW,
        )


def test_modified_signature_is_rejected(material, google_settings, google_rsa_private_key):
    transaction, claims, _ = material
    token = signed_token(claims, google_rsa_private_key)
    parts = token.split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    with pytest.raises(GoogleIdentityError):
        verify_google_id_token(
            ".".join(parts),
            ACCESS_TOKEN,
            KeySet.import_key_set(public_jwks(google_rsa_private_key)),
            transaction,
            google_settings,
            now=NOW,
        )


@pytest.mark.parametrize("update", [{"use": "enc"}, {"alg": "RS384"}, {"key_ops": ["sign"]}])
def test_key_usage_and_algorithm_are_enforced(material, google_rsa_private_key, update):
    _, _, verify = material
    jwks = public_jwks(google_rsa_private_key)
    jwks["keys"][0].update(update)
    if update.get("use") == "enc":
        jwks["keys"][0].pop("key_ops", None)
    with pytest.raises(GoogleIdentityError):
        verify(keyset=KeySet.import_key_set(jwks))


def test_incompatible_key_and_duplicate_key_ids_are_rejected(material, google_rsa_private_key):
    _, _, verify = material
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

    other = generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(GoogleIdentityError):
        verify(keyset=KeySet.import_key_set(public_jwks(other)))
    jwks = public_jwks(google_rsa_private_key)
    jwks["keys"].append(dict(jwks["keys"][0]))
    with pytest.raises(GoogleIdentityError):
        verify(keyset=KeySet.import_key_set(jwks))


@pytest.mark.parametrize("token", ["", "fixture", "a.b.c", "x" * 16385, "☃.a.b"])
def test_malformed_tokens_have_safe_errors(material, google_settings, google_rsa_private_key, token):
    transaction, _, _ = material
    with pytest.raises(GoogleIdentityError) as caught:
        verify_google_id_token(
            token,
            ACCESS_TOKEN,
            KeySet.import_key_set(public_jwks(google_rsa_private_key)),
            transaction,
            google_settings,
            now=NOW,
        )
    if token:
        assert token not in str(caught.value)


def test_naive_clock_rejected_and_non_utc_clock_supported(material):
    _, claims, verify = material
    with pytest.raises(GoogleIdentityError):
        verify(now=NOW.replace(tzinfo=None))
    assert verify(now=NOW.astimezone(timezone(timedelta(hours=9)))).subject == claims["sub"]


def test_nonce_from_another_login_is_rejected(material):
    _, claims, verify = material
    other = create_google_transaction(now=NOW)
    with pytest.raises(GoogleIdentityError):
        verify({**claims, "nonce": other.nonce})


@pytest.mark.parametrize("claims", [None, [], ["iss"], "fixture-private-payload"])
def test_non_object_claims_are_rejected(material, google_settings, google_rsa_private_key, claims):
    transaction, _, _ = material
    token = signed_token(claims, google_rsa_private_key)
    with pytest.raises(GoogleIdentityError):
        verify_google_id_token(
            token,
            ACCESS_TOKEN,
            KeySet.import_key_set(public_jwks(google_rsa_private_key)),
            transaction,
            google_settings,
            now=NOW,
        )


@pytest.mark.parametrize("access_token", [None, "", "a b", "☃"])
def test_missing_or_invalid_access_cannot_skip_optional_hash_verification(
    material, google_settings, google_rsa_private_key, access_token
):
    transaction, claims, _ = material
    token = signed_token({**claims, "at_hash": "wrong"}, google_rsa_private_key)
    with pytest.raises(GoogleIdentityError):
        verify_google_id_token(
            token,
            access_token,
            KeySet.import_key_set(public_jwks(google_rsa_private_key)),
            transaction,
            google_settings,
            now=NOW,
        )


def test_identity_uses_subject_when_verified_email_changes(material):
    _, claims, verify = material
    first = verify()
    changed = verify({**claims, "email": "changed@example.test"})
    assert first.subject == changed.subject and first.issuer == changed.issuer
    assert first.email != changed.email
