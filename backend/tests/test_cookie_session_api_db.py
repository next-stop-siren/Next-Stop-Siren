"""Real HTTP cookie lifecycle and post-commit response recovery in guarded PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError
from app.main import create_app
from app.models.refresh_session import RefreshSession
from app.security.session_values import create_csrf_token
from app.security.tokens import verify_access_token
from app.services.refresh import rotate_refresh_session
from cookie_session_helpers import seed_auth_cookies
from test_refresh_rotation_db import snapshot, stored as stored

pytestmark = pytest.mark.db
MODULE = "app.api.cookie_sessions"
ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture
def api_db(stored, auth_settings):
    factory = stored[0]
    app = create_app(auth_settings)
    with patch("app.db._session_factory", factory), TestClient(app, base_url="http://127.0.0.1:8000") as client:
        seed_auth_cookies(client, stored[2], auth_settings)
        yield SimpleNamespace(client=client, app=app, factory=factory, stored=stored, settings=auth_settings)


def headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["csrf_token"]}


def assert_expired(response):
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("Max-Age=0" in cookie and "Path=/api/auth" in cookie and "Domain=" not in cookie for cookie in cookies)


def test_csrf_refresh_me_logout_lifecycle(api_db):
    api = api_db
    before = snapshot(api.stored)
    csrf = api.client.get("/api/auth/csrf")
    assert csrf.status_code == 200 and csrf.json() == {"csrf_token": api.client.cookies["csrf_token"]}
    assert "set-cookie" not in csrf.headers and snapshot(api.stored) == before
    refreshed = api.client.post("/api/auth/refresh", headers=headers(api.client))
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "csrf_token"}
    assert body["csrf_token"] == api.client.cookies["csrf_token"]
    assert api.client.cookies["refresh_token"] != api.stored[2].token
    bearer = {"Authorization": f"Bearer {body['access_token']}"}
    assert api.client.get("/api/me", headers=bearer).status_code == 200
    logged_out = api.client.post("/api/auth/logout", headers=headers(api.client))
    assert logged_out.status_code == 204 and logged_out.content == b""
    assert_expired(logged_out)
    assert not api.client.cookies
    assert api.client.get("/api/auth/csrf").status_code == 401
    assert api.client.get("/api/me", headers=bearer).status_code == 200
    assert verify_access_token(body["access_token"], api.settings) == api.stored[1]
    with api.factory() as reader:
        assert all(reader.get(RefreshSession, identifier).revoked_at is None for identifier in api.stored[4])


def test_simultaneous_http_refresh_has_single_winner_and_no_loser_cookies(api_db):
    api = api_db
    barrier = Barrier(2)

    def worker(_):
        with TestClient(api.app, base_url="http://127.0.0.1:8000") as client:
            seed_auth_cookies(client, api.stored[2], api.settings)
            barrier.wait(timeout=10)
            return client.post("/api/auth/refresh", headers=headers(client))

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(worker, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["error"]["code"] == "refresh_race"
    assert loser.headers["retry-after"] == "1" and "set-cookie" not in loser.headers
    with api.factory() as reader:
        children = list(reader.scalars(select(RefreshSession).where(RefreshSession.rotated_from_id == api.stored[3])))
        assert len(children) == 1 and children[0].revoked_at is None


def test_old_pair_outside_window_is_401_and_clears_only_its_lineage(api_db):
    api = api_db
    assert api.client.post("/api/auth/refresh", headers=headers(api.client)).status_code == 200
    with api.factory() as reader:
        revoked_at = reader.get(RefreshSession, api.stored[3]).revoked_at
    with TestClient(api.app, base_url="http://127.0.0.1:8000") as stale:
        seed_auth_cookies(stale, api.stored[2], api.settings)
        with patch("app.services.refresh.datetime") as clock:
            clock.now.return_value = revoked_at + timedelta(seconds=6)
            response = stale.post("/api/auth/refresh", headers=headers(stale))
        assert response.status_code == 401
        assert_expired(response)
        assert not stale.cookies
    assert api.client.get("/api/auth/csrf").status_code == 401
    with api.factory() as reader:
        rows = list(reader.scalars(select(RefreshSession)))
        assert all((row.revoked_at is None) == (row.id in api.stored[4]) for row in rows)


def test_issuance_limit_http_response_follows_committed_revocation(api_db):
    # 4095 경계는 서비스 DB 테스트에서 검증했다. 여기서는 한도를 낮춰 HTTP 변환만 확인한다.
    api = api_db
    with patch("app.services.refresh.MAX_REFRESH_LINEAGE_ISSUANCES", 1):
        response = api.client.post("/api/auth/refresh", headers=headers(api.client))
    assert response.status_code == 401
    assert_expired(response)
    with api.factory() as reader:
        assert reader.get(RefreshSession, api.stored[3]).revoked_at is not None
        assert all(reader.get(RefreshSession, identifier).revoked_at is None for identifier in api.stored[4])
        assert len(list(reader.scalars(select(RefreshSession)))) == 3


@pytest.mark.parametrize("path", ["csrf", "refresh", "logout"])
def test_corrupt_metadata_is_503_without_cookies_or_state_changes(api_db, path, caplog):
    api = api_db
    with api.factory() as session:
        session.execute(
            update(RefreshSession)
            .where(RefreshSession.id == api.stored[3])
            .values(issued_at=api.stored[2].issued_at - timedelta(seconds=1))
        )
        session.commit()
    before = snapshot(api.stored)
    response = (
        api.client.get("/api/auth/csrf")
        if path == "csrf"
        else api.client.post(f"/api/auth/{path}", headers=headers(api.client))
    )
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert response.json()["error"]["code"] == "temporarily_unavailable"
    assert response.json()["error"]["trace_id"] in caplog.text
    assert api.stored[2].token not in caplog.text
    assert snapshot(api.stored) == before


@pytest.mark.parametrize("path", ["refresh", "logout"])
def test_commit_failure_http_never_sends_success_or_cookies(api_db, path):
    api = api_db
    before = snapshot(api.stored)

    def failed_commit(session):
        session.flush()
        raise OperationalError("private SQL", None, None)

    with patch.object(Session, "commit", failed_commit):
        response = api.client.post(f"/api/auth/{path}", headers=headers(api.client))
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert "access_token" not in response.text and "private" not in response.text
    assert snapshot(api.stored) == before


@pytest.mark.parametrize("rotate_during_preparation", [False, True])
def test_partial_cookie_failure_revokes_unreturned_successor_and_descendants(api_db, rotate_during_preparation, caplog):
    api = api_db
    caplog.set_level("INFO", logger="app.api.cookie_sessions")
    unreturned = []

    def partial(response, refresh, settings):
        unreturned.append(refresh.token)
        response.set_cookie("refresh_token", refresh.token)
        if rotate_during_preparation:
            with api.factory() as session:
                child = rotate_refresh_session(refresh.token, session, settings)
            unreturned.append(child.refresh.token)
        raise AuthCookieError("private cookie details")

    with patch(f"{MODULE}.set_auth_cookies", side_effect=partial):
        response = api.client.post("/api/auth/refresh", headers=headers(api.client))
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert "event=refresh_cancelled" in caplog.text
    assert all(token not in response.text and token not in caplog.text for token in unreturned)
    with api.factory() as reader:
        rows = list(reader.scalars(select(RefreshSession)))
        assert len(rows) == (5 if rotate_during_preparation else 4)
        assert all((row.revoked_at is None) == (row.id in api.stored[4]) for row in rows)


def test_compensation_failure_never_claims_revocation_or_leaks_new_credentials(api_db, caplog):
    api = api_db
    with (
        patch(f"{MODULE}.set_auth_cookies", side_effect=AuthCookieError("private cookie")),
        patch("app.services.refresh.revoke_refresh_lineage", side_effect=OperationalError("private SQL", None, None)),
    ):
        response = api.client.post("/api/auth/refresh", headers=headers(api.client))
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert "event=refresh_compensation_failed" in caplog.text and "event=refresh_cancelled" not in caplog.text
    assert response.json()["error"]["trace_id"] in caplog.text
    assert "private" not in caplog.text and "access_token" not in response.text
    with api.factory() as reader:
        rows = list(reader.scalars(select(RefreshSession)))
        assert len(rows) == 4
        assert reader.get(RefreshSession, api.stored[3]).revoked_at is not None
        assert sum(row.revoked_at is None for row in rows) == 3


def test_stale_csrf_header_is_403_and_does_not_revoke_new_session(api_db):
    api = api_db
    stale = create_csrf_token(api.stored[2].jti, api.settings)
    assert api.client.post("/api/auth/refresh", headers=headers(api.client)).status_code == 200
    before = snapshot(api.stored)
    response = api.client.post("/api/auth/refresh", headers={"Origin": ORIGIN, "X-CSRF-Token": stale})
    assert response.status_code == 403 and "set-cookie" not in response.headers
    assert snapshot(api.stored) == before
    recovered = api.client.get("/api/auth/csrf")
    assert recovered.status_code == 200
    response = api.client.post(
        "/api/auth/refresh",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": recovered.json()["csrf_token"],
        },
    )
    assert response.status_code == 200
