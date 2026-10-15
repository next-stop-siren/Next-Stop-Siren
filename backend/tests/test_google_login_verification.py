"""Google callback verification workflow with mocked HTTP and real RSA validation."""

import asyncio
from dataclasses import replace
import logging
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest

from app.integrations.google_oidc import GOOGLE_AUTHORIZATION_ENDPOINT, GoogleProviderUnavailable, GoogleTokenResponse
from app.security.google_identity import GoogleIdentityError
from app.security.google_transactions import (
    GoogleTransactionError,
    create_google_transaction,
    encode_google_transaction,
)
from app.services.google_login import verify_google_login
from google_helpers import ACCESS_TOKEN, CODE, KID, NOW, google_claims, public_jwks, signed_token


@pytest.fixture
def flow(google_settings, google_rsa_private_key):
    transaction = create_google_transaction(now=NOW)
    cookie = encode_google_transaction(transaction, google_settings, now=NOW)
    claims = google_claims(transaction, google_settings)
    token = signed_token(claims, google_rsa_private_key)
    calls = []
    behavior = {
        "discovery": {
            "issuer": google_settings.issuer,
            "authorization_endpoint": GOOGLE_AUTHORIZATION_ENDPOINT,
            "token_endpoint": google_settings.token_endpoint,
            "jwks_uri": google_settings.jwks_uri,
            "id_token_signing_alg_values_supported": ["RS256"],
        },
        "exchange": {"id_token": token, "access_token": ACCESS_TOKEN, "token_type": "Bearer"},
        "jwks": public_jwks(google_rsa_private_key),
    }
    stages = {
        google_settings.discovery_url: "discovery",
        google_settings.token_endpoint: "exchange",
        google_settings.jwks_uri: "jwks",
    }
    used_codes = set()

    async def handler(request):
        calls.append(request)
        assert str(request.url) in stages
        stage = stages[str(request.url)]
        if behavior.get("exception_stage") == stage:
            raise httpx.ReadTimeout("fixture-private-provider-error")
        if behavior.get("delay_stage") == stage:
            await asyncio.sleep(0.1)
        if stage == "exchange":
            form = parse_qs(request.content.decode())
            if form.get("code_verifier") != [transaction.code_verifier]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if behavior.get("single_use"):
                if form["code"][0] in used_codes:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                used_codes.add(form["code"][0])
        status = behavior.get(stage + "_status", 200)
        if behavior.get("raw_stage") == stage:
            return httpx.Response(status, content=b"fixture-private-not-json")
        return httpx.Response(status, json=behavior[stage])

    def run(**overrides):
        async def invoke():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await verify_google_login(
                    overrides.get("code", CODE),
                    overrides.get("state", transaction.state),
                    overrides.get("cookie_value", cookie),
                    google_settings,
                    client,
                    now=NOW,
                )

        return asyncio.run(invoke())

    return SimpleNamespace(
        transaction=transaction,
        cookie=cookie,
        claims=claims,
        token=token,
        calls=calls,
        behavior=behavior,
        run=run,
    )


def test_verified_login_sends_exact_exchange_and_exposes_only_identity(flow, google_settings, caplog):
    with caplog.at_level(logging.INFO):
        result = flow.run()
    assert result.identity.issuer == google_settings.issuer
    assert result.identity.subject == flow.claims["sub"] and result.identity.email == flow.claims["email"]
    assert result.return_to == "/"
    assert [(call.method, str(call.url)) for call in flow.calls] == [
        ("GET", google_settings.discovery_url),
        ("POST", google_settings.token_endpoint),
        ("GET", google_settings.jwks_uri),
    ]
    request = flow.calls[1]
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.url.query == b""
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["authorization_code"],
        "code": [CODE],
        "redirect_uri": [google_settings.redirect_uri],
        "client_id": [google_settings.client_id],
        "client_secret": [google_settings.client_secret],
        "code_verifier": [flow.transaction.code_verifier],
    }
    for secret in (CODE, google_settings.client_secret, flow.token, ACCESS_TOKEN, flow.transaction.code_verifier):
        assert secret not in repr(result) and secret not in caplog.text
    tokens = GoogleTokenResponse(id_token=flow.token, access_token=ACCESS_TOKEN)
    assert flow.token not in repr(tokens) and ACCESS_TOKEN not in repr(tokens)


@pytest.mark.parametrize(
    "name,value",
    [
        ("state", None),
        ("state", "wrong"),
        ("cookie_value", None),
        ("cookie_value", "invalid-cookie"),
        ("code", None),
        ("code", ""),
        ("code", "a b"),
        ("code", "☃"),
        ("code", "x" * 4097),
    ],
)
def test_invalid_transaction_or_code_never_contacts_google(flow, name, value):
    with pytest.raises(GoogleIdentityError):
        flow.run(**{name: value})
    assert flow.calls == []


def test_expired_transaction_never_contacts_google(flow, google_settings):
    expired = replace(
        flow.transaction, issued_at=flow.transaction.issued_at - 300, expires_at=flow.transaction.expires_at - 300
    )
    from datetime import timedelta

    cookie = encode_google_transaction(expired, google_settings, now=NOW - timedelta(seconds=300))
    with pytest.raises(GoogleIdentityError):
        flow.run(cookie_value=cookie)
    assert flow.calls == []


def test_wrong_pkce_verifier_is_rejected_by_token_exchange_without_jwks_lookup(flow, google_settings):
    wrong = replace(flow.transaction, code_verifier="x" * 43)
    cookie = encode_google_transaction(wrong, google_settings, now=NOW)
    with pytest.raises(GoogleIdentityError):
        flow.run(cookie_value=cookie)
    assert [request.method for request in flow.calls] == ["GET", "POST"]


def test_replayed_code_is_rejected_and_post_is_never_retried(flow):
    flow.behavior["single_use"] = True
    assert flow.run().identity.subject == flow.claims["sub"]
    with pytest.raises(GoogleIdentityError):
        flow.run()
    assert sum(request.method == "POST" for request in flow.calls) == 2
    assert len(flow.calls) == 5


@pytest.mark.parametrize("error", ["invalid_grant", "access_denied"])
def test_code_rejection_is_auth_failure_and_hides_google_description(flow, error):
    flow.behavior["exchange_status"] = 400
    flow.behavior["exchange"] = {"error": error, "error_description": "fixture-private-details"}
    with pytest.raises(GoogleIdentityError) as caught:
        flow.run()
    assert "fixture-private" not in str(caught.value)
    assert len(flow.calls) == 2


@pytest.mark.parametrize("stage", ["discovery", "exchange", "jwks"])
@pytest.mark.parametrize("kind", ["http", "timeout", "raw"])
def test_provider_failures_are_distinct_safe_and_not_retried(flow, stage, kind, caplog):
    if kind == "http":
        flow.behavior[stage + "_status"] = 503
    elif kind == "timeout":
        flow.behavior["exception_stage"] = stage
    else:
        flow.behavior["raw_stage"] = stage
    with caplog.at_level(logging.INFO), pytest.raises(GoogleProviderUnavailable) as caught:
        flow.run()
    assert (
        caught.value.failure.stage
        == {"discovery": "google_discovery", "exchange": "google_code_exchange", "jwks": "google_jwks"}[stage]
    )
    assert sum(request.method == "POST" for request in flow.calls) <= 1
    assert "fixture-private" not in str(caught.value) and "fixture-private" not in caplog.text


@pytest.mark.parametrize("stage", ["discovery", "exchange", "jwks"])
def test_total_timeout_applies_to_every_google_stage(flow, stage):
    flow.behavior["delay_stage"] = stage
    with patch("app.integrations.google_oidc.GOOGLE_DISCOVERY_TIMEOUT_SECONDS", 0.001):
        with pytest.raises(GoogleProviderUnavailable):
            flow.run()


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "https://evil.test"),
        ("token_endpoint", "https://evil.test/token"),
        ("jwks_uri", "https://evil.test/keys"),
        ("id_token_signing_alg_values_supported", ["HS256"]),
        ("id_token_signing_alg_values_supported", "RS256"),
    ],
)
def test_discovery_is_checked_before_sending_code_or_secret(flow, field, value):
    flow.behavior["discovery"][field] = value
    with pytest.raises(GoogleProviderUnavailable):
        flow.run()
    assert len(flow.calls) == 1 and flow.calls[0].method == "GET"


def test_invalid_client_is_configuration_failure_not_invalid_credentials(flow):
    flow.behavior["exchange_status"] = 401
    flow.behavior["exchange"] = {"error": "invalid_client", "error_description": "fixture-private-client-secret"}
    with pytest.raises(GoogleProviderUnavailable) as caught:
        flow.run()
    assert caught.value.failure.stage == "google_code_exchange"
    assert len(flow.calls) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("id_token", None),
        ("id_token", ""),
        ("id_token", 123),
        ("id_token", "x" * 16385),
        ("access_token", None),
        ("access_token", ""),
        ("access_token", "☃"),
        ("token_type", None),
        ("token_type", "MAC"),
    ],
)
def test_missing_or_invalid_tokens_return_no_identity(flow, field, value):
    flow.behavior["exchange"][field] = value
    with pytest.raises(GoogleIdentityError):
        flow.run()
    assert len(flow.calls) == 2


@pytest.mark.parametrize("payload", [None, [], {}, {"keys": []}, {"keys": [None]}, {"keys": [{"kty": "RSA"}]}])
def test_malformed_jwks_is_a_provider_failure(flow, payload):
    flow.behavior["jwks"] = payload
    with pytest.raises(GoogleProviderUnavailable) as caught:
        flow.run()
    assert caught.value.failure.stage == "google_jwks"


def test_jwks_private_keys_and_duplicate_ids_are_rejected(flow, google_rsa_private_key):
    import json
    import jwt

    private = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(google_rsa_private_key))
    private["kid"] = KID
    for keys in ([private], flow.behavior["jwks"]["keys"] * 2):
        flow.behavior["jwks"] = {"keys": keys}
        with pytest.raises(GoogleProviderUnavailable):
            flow.run()


def test_token_header_urls_are_never_fetched(flow, google_rsa_private_key):
    flow.behavior["exchange"]["id_token"] = signed_token(
        flow.claims, google_rsa_private_key, header={"jku": "https://evil.test/keys"}
    )
    with pytest.raises(GoogleIdentityError):
        flow.run()
    assert len(flow.calls) == 3
    assert all("evil.test" not in str(request.url) for request in flow.calls)


def test_claim_validation_failure_returns_no_identity(flow, google_rsa_private_key):
    flow.behavior["exchange"]["id_token"] = signed_token(
        {**flow.claims, "email_verified": False}, google_rsa_private_key
    )
    with pytest.raises(GoogleIdentityError):
        flow.run()


def test_key_rotation_uses_fresh_jwks_for_next_login(flow):
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

    assert flow.run().identity.subject == flow.claims["sub"]
    new_key = generate_private_key(public_exponent=65537, key_size=2048)
    flow.behavior["jwks"] = public_jwks(new_key, kid="rotated-key")
    flow.behavior["exchange"]["id_token"] = signed_token(flow.claims, new_key, header={"kid": "rotated-key"})
    assert flow.run().identity.subject == flow.claims["sub"]
    assert len(flow.calls) == 6


def test_transaction_expiring_during_network_calls_is_rechecked(flow):
    with patch(
        "app.services.google_login.verify_google_transaction", side_effect=[flow.transaction, GoogleTransactionError()]
    ):
        with pytest.raises(GoogleIdentityError):
            flow.run()
    assert len(flow.calls) == 3
