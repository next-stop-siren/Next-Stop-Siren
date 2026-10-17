"""Current-lineage logout, independent-login preservation, and rollback in guarded PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Annotated
from unittest.mock import patch

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.auth import require_refresh_csrf
from app.api.errors import register_error_handlers
from app.models.refresh_session import RefreshSession
from app.security.session_values import create_csrf_token, create_refresh_digest
from app.security.tokens import VerifiedToken, issue_access_token, verify_access_token
from app.services.logout import logout_refresh_session
from app.services.refresh import InvalidRefresh, RefreshResult, RefreshUnavailable, rotate_refresh_session
from test_refresh_rotation_db import snapshot, stored as stored

pytestmark = pytest.mark.db


def rotate(stored, auth_settings):
    with stored[0]() as session:
        return rotate_refresh_session(stored[2].token, session, auth_settings)


def logout(stored, auth_settings, token=None):
    with stored[0]() as session:
        logout_refresh_session(token or stored[2].token, session, auth_settings)
        assert not session.in_transaction()


@pytest.mark.parametrize("rotated", [False, True])
def test_logout_commits_only_target_lineage_and_preserves_access(stored, auth_settings, rotated):
    factory, user_id, original, _, other_ids = stored
    if rotated:
        issued = rotate(stored, auth_settings)
        token, access = issued.refresh.token, issued.access.token
    else:
        token, access = original.token, issue_access_token(user_id, auth_settings).token
    before = snapshot(stored)
    logout(stored, auth_settings, token)
    after = snapshot(stored)
    assert len(after) == len(before)
    for previous, current in zip(before, after, strict=True):
        if current[0] in other_ids:
            assert current == previous and current[-1] is None
        else:
            assert current[:-1] == previous[:-1] and current[-1] is not None
            if previous[-1] is not None:
                assert current[-1] == previous[-1]
    assert verify_access_token(access, auth_settings) == user_id
    with factory() as session:
        with pytest.raises(InvalidRefresh):
            rotate_refresh_session(token, session, auth_settings)
    with pytest.raises(InvalidRefresh):
        logout(stored, auth_settings, token)
    assert snapshot(stored) == after


def test_old_rotated_token_cannot_log_out_active_successor(stored, auth_settings):
    current = rotate(stored, auth_settings)
    before = snapshot(stored)
    with pytest.raises(InvalidRefresh):
        logout(stored, auth_settings)
    assert snapshot(stored) == before
    logout(stored, auth_settings, current.refresh.token)


def test_commit_failure_restores_current_lineage_and_allows_retry(stored, auth_settings):
    current = rotate(stored, auth_settings)
    before = snapshot(stored)

    def failed_commit(session):
        session.flush()
        raise OperationalError("private SQL", None, RuntimeError("private failure"))

    with patch.object(Session, "commit", failed_commit):
        with pytest.raises(RefreshUnavailable) as caught:
            logout(stored, auth_settings, current.refresh.token)
    assert caught.value.failure.stage == "commit"
    assert "private" not in str(caught.value)
    assert snapshot(stored) == before
    logout(stored, auth_settings, current.refresh.token)


def test_cycle_is_rejected_without_claiming_successful_logout(stored, auth_settings, caplog):
    factory, _, _, root_id, _ = stored
    current = rotate(stored, auth_settings)
    with factory() as session:
        child_id = session.scalar(
            select(RefreshSession.id).where(
                RefreshSession.token_digest == create_refresh_digest(current.refresh.token, auth_settings)
            )
        )
        session.execute(update(RefreshSession).where(RefreshSession.id == root_id).values(rotated_from_id=child_id))
        session.commit()
    before = snapshot(stored)
    with pytest.raises(RefreshUnavailable) as caught:
        logout(stored, auth_settings, current.refresh.token)
    assert caught.value.failure.error_type == "CorruptRefreshLineage"
    assert "event=logout_lineage_corrupt" in caplog.text
    assert current.refresh.token not in caplog.text
    assert snapshot(stored) == before


@pytest.mark.parametrize("failure", ["origin", "csrf"])
def test_origin_or_csrf_failure_never_reaches_logout(stored, auth_settings, failure):
    factory, _, original, _, _ = stored
    before = snapshot(stored)
    app = FastAPI()
    app.state.auth_settings = auth_settings
    register_error_handlers(app)
    reached = []

    @app.post("/api/auth/logout-probe")
    def probe(request: Request, verified: Annotated[VerifiedToken, Depends(require_refresh_csrf)]):
        reached.append(True)
        with factory() as session:
            logout_refresh_session(request.cookies["refresh_token"], session, auth_settings)
        return {"ok": True}

    csrf = create_csrf_token(original.jti, auth_settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.cookies.set("refresh_token", original.token, path="/api/auth")
        client.cookies.set("csrf_token", csrf, path="/api/auth")
        response = client.post(
            "/api/auth/logout-probe",
            headers={
                "Origin": "http://evil.example" if failure == "origin" else "http://127.0.0.1:5173",
                "X-CSRF-Token": "wrong" if failure == "csrf" else csrf,
            },
        )
    assert response.status_code == 403 and response.json()["error"]["code"] == "csrf_failed"
    assert "set-cookie" not in response.headers
    assert not reached
    assert snapshot(stored) == before


def test_concurrent_logout_and_rotation_follow_same_user_lock(stored, auth_settings):
    factory, _, original, _, other_ids = stored
    barrier = Barrier(2)

    def worker(operation):
        with factory() as session:
            barrier.wait(timeout=10)
            try:
                if operation == "logout":
                    logout_refresh_session(original.token, session, auth_settings)
                    return "logged-out"
                return rotate_refresh_session(original.token, session, auth_settings)
            except InvalidRefresh as exc:
                return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, ("logout", "rotate")))
    assert sum(isinstance(item, InvalidRefresh) for item in results) == 1
    with factory() as reader:
        rows = list(reader.scalars(select(RefreshSession).where(RefreshSession.id.not_in(other_ids))))
        if results[0] == "logged-out":
            assert len(rows) == 1 and all(row.revoked_at is not None for row in rows)
        else:
            assert isinstance(results[1], RefreshResult)
            assert len(rows) == 2 and sum(row.revoked_at is None for row in rows) == 1
        assert all(reader.get(RefreshSession, identifier).revoked_at is None for identifier in other_ids)
