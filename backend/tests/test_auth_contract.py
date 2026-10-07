"""HTTP contract examples with mock principals, not production JWT verification.

The probe deliberately accepts a forged body ID to demonstrate principal forwarding.
Production request schemas must reject user_id as specified by docs/04-reference/api.md.
"""

from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def require_test_principal() -> dict[str, int]:
    """Reject requests unless the test explicitly supplies a verified principal."""
    raise HTTPException(status_code=401, detail="로그인이 필요합니다.")


@pytest.fixture
def auth_contract_app(auth_cases: dict[str, Any]):
    """Create an isolated app and spy on the user ID passed to protected processing."""
    app = FastAPI()
    public_users = {u["principal"]["user_id"]: u["public_user"] for u in auth_cases["users"].values()}
    lookup_user = Mock(side_effect=lambda user_id: public_users[user_id])

    @app.exception_handler(HTTPException)
    async def authentication_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "unauthenticated",
                    "message": exc.detail,
                    "fields": {},
                    "trace_id": str(uuid4()),
                }
            },
        )

    @app.get("/api/me")
    def me(principal: dict[str, int] = Depends(require_test_principal)) -> dict[str, Any]:
        return {"user": lookup_user(principal["user_id"])}

    @app.post("/test/principal-probe")
    def principal_probe(
        body: dict[str, Any], principal: dict[str, int] = Depends(require_test_principal)
    ) -> dict[str, Any]:
        # The body is adversarial input; only the mock verified principal reaches lookup.
        return {"user": lookup_user(principal["user_id"])}

    return app, lookup_user


def test_verified_principal_reaches_protected_lookup(auth_contract_app, auth_user, auth_cases):
    app, lookup_user = auth_contract_app
    app.dependency_overrides[require_test_principal] = lambda: auth_user["principal"]
    with TestClient(app) as client:
        response = client.get("/api/me")
    assert response.status_code == auth_cases["expected_success_statuses"]["me"]
    assert response.json() == auth_user["expected_me_response"]
    lookup_user.assert_called_once_with(auth_user["principal"]["user_id"])


def test_body_user_id_cannot_replace_verified_principal(auth_contract_app, auth_cases, principal_body_mismatch_case):
    app, lookup_user = auth_contract_app
    case = principal_body_mismatch_case
    user = auth_cases["users"][case["authenticated_user"]]
    app.dependency_overrides[require_test_principal] = lambda: user["principal"]
    with TestClient(app) as client:
        response = client.post("/test/principal-probe", json=case["request_body"])
    assert response.status_code == 200
    assert response.json() == user["expected_me_response"]
    assert response.json()["user"]["id"] != case["request_body"]["user_id"]
    lookup_user.assert_called_once_with(case["expected_principal_user_id"])


@pytest.mark.parametrize("path", ["/api/me", "/test/principal-probe"])
def test_unauthenticated_requests_return_401_before_protected_lookup(auth_contract_app, unauthenticated_case, path):
    app, lookup_user = auth_contract_app
    with TestClient(app) as client:
        if path == "/api/me":
            response = client.get(path, headers=unauthenticated_case["request_headers"])
        else:
            response = client.post(path, json={"user_id": "101"}, headers=unauthenticated_case["request_headers"])
    assert response.status_code == unauthenticated_case["expected_status"]
    assert response.headers["content-type"] == "application/json"
    payload = response.json()
    expected = unauthenticated_case["expected_response"]["error"]
    assert set(payload) == {"error"}
    assert set(payload["error"]) == set(expected)
    assert {key: value for key, value in payload["error"].items() if key != "trace_id"} == {
        key: value for key, value in expected.items() if key != "trace_id"
    }
    assert isinstance(payload["error"]["trace_id"], str) and payload["error"]["trace_id"]
    lookup_user.assert_not_called()


def test_fake_token_does_not_create_a_verified_principal(auth_contract_app, auth_user_a, unauthenticated_case):
    app, lookup_user = auth_contract_app
    with TestClient(app) as client:
        response = client.get(
            "/api/me", headers={"Authorization": f"Bearer {auth_user_a['expected_auth_response']['access_token']}"}
        )
    assert response.status_code == unauthenticated_case["expected_status"]
    lookup_user.assert_not_called()
