"""S05 acceptance across Google callbacks, local credentials, and Bearer identity isolation."""

from unittest.mock import patch

import jwt
import pytest
from sqlalchemy import func, select

from app.models.auth_identity import AuthIdentity
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.security.session_values import create_refresh_digest

pytestmark = pytest.mark.db
ORIGIN = {"Origin": "http://127.0.0.1:5173"}
CALLBACK = "/api/auth/google/callback"


def refresh_claims(token, settings):
    return jwt.decode(
        token, settings.refresh_key, algorithms=["HS256"], issuer=settings.issuer, audience=settings.audience
    )


@pytest.mark.parametrize("google_first", [True, False])
def test_same_email_accounts_stay_separate_through_registration_login_callback_and_me(
    google_callback_flow, committed_google_sessions, auth_settings, google_first
):
    flow = google_callback_flow
    body = {"email": "fixture@example.test", "password": "fixture-local-password"}

    def google_login():
        response = flow.client.get(CALLBACK, params=flow.params)
        assert response.status_code == 302 and response.headers["location"] == "/"
        assert response.content == b""
        return response.cookies["refresh_token"]

    def register():
        response = flow.client.post("/api/auth/register", json=body, headers=ORIGIN)
        assert response.status_code == 201
        return response.json()

    with patch("app.db._session_factory", committed_google_sessions):
        if google_first:
            google_refresh = google_login()
            # A Google-only account must not accept local credentials or create a local session.
            rejected = flow.client.post("/api/auth/login", json=body, headers=ORIGIN)
            assert rejected.status_code == 401 and "set-cookie" not in rejected.headers
            local = register()
        else:
            local = register()
            google_refresh = google_login()

        google_id = refresh_claims(google_refresh, auth_settings)["sub"]
        local_id = local["user"]["id"]
        assert google_id != local_id

        # Cookie identity does not override the user selected by the Bearer access token.
        bearer = {"Authorization": f"Bearer {local['access_token']}"}
        me = flow.client.get("/api/me", headers=bearer)
        assert me.status_code == 200 and me.json()["user"]["id"] == local_id
        assert flow.client.get("/api/me").status_code == 401
        assert flow.client.get("/api/me", headers={"Authorization": f"Bearer {google_refresh}"}).status_code == 401

        login = flow.client.post("/api/auth/login", json=body, headers=ORIGIN)
        assert login.status_code == 200 and login.json()["user"]["id"] == local_id
        assert refresh_claims(login.cookies["refresh_token"], auth_settings)["sub"] == local_id
        access = jwt.decode(
            login.json()["access_token"],
            auth_settings.access_key,
            algorithms=["HS256"],
            issuer=auth_settings.issuer,
            audience=auth_settings.audience,
        )
        assert access["sub"] == local_id and access["token_type"] == "access"

        flow.start(code="fixture-acceptance-relogin-code")
        new_google_refresh = google_login()
        claims = refresh_claims(new_google_refresh, auth_settings)
        assert claims["sub"] == google_id and new_google_refresh != google_refresh
        assert claims["jti"] != refresh_claims(google_refresh, auth_settings)["jti"]
        assert flow.client.get("/api/me", headers=bearer).json()["user"]["id"] == local_id

        with committed_google_sessions() as reader:
            assert reader.scalar(select(func.count()).select_from(User)) == 2
            assert reader.scalar(select(func.count()).select_from(AuthIdentity)) == 1
            assert reader.scalar(select(func.count()).select_from(RefreshSession)) == 4
            assert reader.scalar(select(AuthIdentity.user_id)) == int(google_id)
            assert reader.get(User, int(google_id)).password_hash is None
            assert reader.get(User, int(local_id)).password_hash is not None
            for token, user_id in (
                (google_refresh, google_id),
                (new_google_refresh, google_id),
                (login.cookies["refresh_token"], local_id),
            ):
                stored = reader.scalar(
                    select(RefreshSession).where(
                        RefreshSession.token_digest == create_refresh_digest(token, auth_settings)
                    )
                )
                assert stored.user_id == int(user_id) and stored.rotated_from_id is None
