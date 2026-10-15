"""An actual start/callback HTTP flow with mocked Google HTTP and real RSA verification."""

from datetime import datetime, timezone
from threading import Lock
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest

from app.api.google_auth import google_http_client
from app.api.google_cookies import GOOGLE_TRANSACTION_COOKIE_NAME
from app.integrations.google_oidc import GOOGLE_AUTHORIZATION_ENDPOINT
from app.security.google_transactions import verify_google_transaction
from google_helpers import ACCESS_TOKEN, CODE, google_claims, public_jwks, signed_token


@pytest.fixture
def google_callback_flow(auth_settings, google_settings, google_rsa_private_key):
    from app.main import create_app

    calls = []
    behavior = {}
    used_codes = set()
    exchange_lock = Lock()
    flow = SimpleNamespace(calls=calls, behavior=behavior, settings=google_settings)
    metadata = {
        "issuer": google_settings.issuer,
        "authorization_endpoint": GOOGLE_AUTHORIZATION_ENDPOINT,
        "token_endpoint": google_settings.token_endpoint,
        "jwks_uri": google_settings.jwks_uri,
        "id_token_signing_alg_values_supported": ["RS256"],
    }

    async def handler(request):
        calls.append(request)
        if str(request.url) == google_settings.discovery_url:
            if behavior.get("discovery_failure"):
                raise httpx.ReadTimeout("fixture-private-provider")
            return httpx.Response(200, json=metadata)
        if str(request.url) == google_settings.jwks_uri:
            return httpx.Response(200, json=public_jwks(google_rsa_private_key))
        assert str(request.url) == google_settings.token_endpoint and request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["client_secret"] == [google_settings.client_secret]
        assert form["redirect_uri"] == [google_settings.redirect_uri]
        if behavior.get("exchange_error"):
            return httpx.Response(400, json={"error": behavior["exchange_error"]})
        with exchange_lock:
            code = form["code"][0]
            if code in used_codes or form["code_verifier"] != [flow.transaction.code_verifier]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            used_codes.add(code)
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            **google_claims(flow.transaction, google_settings),
            "iat": now,
            "exp": now + 3600,
            **behavior.get("claims", {}),
        }
        flow.id_token = signed_token(claims, google_rsa_private_key)
        return httpx.Response(
            200,
            json={
                "id_token": flow.id_token,
                "access_token": ACCESS_TOKEN,
                "token_type": "Bearer",
            },
        )

    async def mock_http_client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            yield client

    app = create_app(auth_settings, google_settings)
    app.dependency_overrides[google_http_client] = mock_http_client
    with TestClient(
        app, base_url=auth_settings.api_origin, follow_redirects=False, client=("192.0.2.1", 123)
    ) as client:
        flow.client = client
        flow.app = app

        def start(code=CODE):
            response = client.get("/api/auth/google/start")
            assert response.status_code == 302
            state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
            flow.cookie = response.cookies[GOOGLE_TRANSACTION_COOKIE_NAME]
            flow.transaction = verify_google_transaction(flow.cookie, state, google_settings)
            flow.params = {"state": state, "code": code}
            calls.clear()
            return flow.params

        flow.start = start
        start()
        yield flow
