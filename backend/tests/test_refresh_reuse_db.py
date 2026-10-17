"""Concurrent rotation, replay revocation, CSRF isolation, and issuance bounds in PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from typing import Annotated
from unittest.mock import patch
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.auth import require_refresh_csrf
from app.api.errors import register_error_handlers
from app.models.refresh_session import RefreshSession
from app.repositories.users import add_local_user
from app.security.session_values import create_csrf_token, create_refresh_digest
from app.security.tokens import VerifiedToken, issue_refresh_token
from app.services.refresh import (
    InvalidRefresh,
    RefreshLimitReached,
    RefreshRace,
    RefreshResult,
    RefreshReuseDetected,
    RefreshUnavailable,
    rotate_refresh_session,
)
from test_refresh_rotation_db import snapshot, stored as stored

pytestmark = pytest.mark.db
MODULE = "app.services.refresh"


def rotate(stored, auth_settings, token=None):
    with stored[0]() as session:
        return rotate_refresh_session(token or stored[2].token, session, auth_settings)


def test_simultaneous_rotation_has_one_winner_and_one_successor(stored, auth_settings):
    factory, _, original, original_id, _ = stored
    barrier = Barrier(2)

    def worker():
        with factory() as session:
            barrier.wait(timeout=10)
            try:
                return rotate_refresh_session(original.token, session, auth_settings)
            except RefreshRace as exc:
                return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: worker(), range(2)))
    assert sum(isinstance(item, RefreshResult) for item in outcomes) == 1
    assert sum(isinstance(item, RefreshRace) for item in outcomes) == 1
    with factory() as session:
        children = list(session.scalars(select(RefreshSession).where(RefreshSession.rotated_from_id == original_id)))
        assert len(children) == 1 and children[0].revoked_at is None
        assert session.get(RefreshSession, original_id).revoked_at is not None


@pytest.mark.parametrize("elapsed", [5, 5.000001])
def test_replay_boundary_preserves_other_logins_and_revocation_times(stored, auth_settings, elapsed):
    factory, _, original, original_id, other_ids = stored
    first = rotate(stored, auth_settings)
    second = rotate(stored, auth_settings, first.refresh.token)
    with factory() as reader:
        revoked_at = reader.get(RefreshSession, original_id).revoked_at
    before = snapshot(stored)
    with patch(f"{MODULE}.datetime") as clock:
        clock.now.return_value = revoked_at + timedelta(seconds=elapsed)
        expected = RefreshRace if elapsed == 5 else RefreshReuseDetected
        with factory() as session:
            with pytest.raises(expected):
                rotate_refresh_session(original.token, session, auth_settings)
            assert not session.in_transaction()
    after = snapshot(stored)
    if elapsed == 5:
        assert after == before
    else:
        assert len(after) == len(before)
        old_by_id = {row[0]: row for row in before}
        for row in after:
            if row[0] in other_ids:
                assert row == old_by_id[row[0]] and row[-1] is None
            else:
                assert row[-1] == (old_by_id[row[0]][-1] or revoked_at + timedelta(seconds=elapsed))
        with pytest.raises(InvalidRefresh):
            rotate(stored, auth_settings, second.refresh.token)
        # 폐기된 계보를 다시 제출해도 409 경합으로 되살리지 않는다.
        with pytest.raises(InvalidRefresh):
            rotate(stored, auth_settings)


def test_replay_revocation_commit_failure_is_unavailable_and_rollback_is_complete(stored, auth_settings):
    factory, _, original, original_id, _ = stored
    current = rotate(stored, auth_settings)
    with factory() as session:
        revoked_at = session.get(RefreshSession, original_id).revoked_at
    before = snapshot(stored)

    def failed_commit(session):
        session.flush()
        raise OperationalError("private SQL", None, None)

    with patch(f"{MODULE}.datetime") as clock, patch.object(Session, "commit", failed_commit):
        clock.now.return_value = revoked_at + timedelta(seconds=6)
        with pytest.raises(RefreshUnavailable) as caught:
            rotate(stored, auth_settings)
    assert caught.value.failure.stage == "commit"
    assert snapshot(stored) == before
    assert rotate(stored, auth_settings, current.refresh.token).refresh.expires_at == original.expires_at


def test_stale_csrf_returns_403_before_rotation_and_preserves_lineage(stored, auth_settings):
    factory, _, original, _, _ = stored
    fresh = rotate(stored, auth_settings).refresh
    before = snapshot(stored)
    app = FastAPI()
    app.state.auth_settings = auth_settings
    register_error_handlers(app)

    @app.post("/api/auth/phase5-probe")
    def probe(request: Request, verified: Annotated[VerifiedToken, Depends(require_refresh_csrf)]):
        with factory() as session:
            rotate_refresh_session(request.cookies["refresh_token"], session, auth_settings)
        return {"ok": True}

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.cookies.set("refresh_token", fresh.token, path="/api/auth")
        client.cookies.set("csrf_token", create_csrf_token(fresh.jti, auth_settings), path="/api/auth")
        response = client.post(
            "/api/auth/phase5-probe",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "X-CSRF-Token": create_csrf_token(original.jti, auth_settings),
            },
        )
    assert response.status_code == 403 and response.json()["error"]["code"] == "csrf_failed"
    assert "set-cookie" not in response.headers
    assert snapshot(stored) == before


@pytest.mark.parametrize("length", [4094, 4095, 4096, 4097])
def test_issuance_limit_revokes_only_valid_target_lineage(committed_signup_sessions, auth_settings, length):
    factory = committed_signup_sessions
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with factory() as session:
        user = add_local_user(session, f"limit-{uuid4().hex}@example.test", "fixture-hash")
        issued_at = now - timedelta(seconds=length)
        token = issue_refresh_token(
            user.id,
            auth_settings,
            now=issued_at + timedelta(seconds=length - 1),
            absolute_expires_at=issued_at + timedelta(days=14),
        )
        ids = list(
            session.scalars(
                insert(RefreshSession).returning(RefreshSession.id, sort_by_parameter_order=True),
                [
                    {
                        "user_id": user.id,
                        "token_digest": create_refresh_digest(token.token, auth_settings)
                        if index == length - 1
                        else uuid4().bytes * 2,
                        "issued_at": issued_at + timedelta(seconds=index),
                        "expires_at": token.expires_at,
                        "revoked_at": None if index == length - 1 else now - timedelta(seconds=1),
                    }
                    for index in range(length)
                ],
            )
        )
        session.execute(
            update(RefreshSession),
            [{"id": ids[index], "rotated_from_id": ids[index - 1]} for index in range(1, length)],
        )
        independent = RefreshSession(
            user_id=user.id, token_digest=uuid4().bytes * 2, issued_at=now, expires_at=now + timedelta(days=14)
        )
        session.add(independent)
        session.commit()
        other_id = independent.id
    with factory() as session:
        if length >= 4097:
            with pytest.raises(RefreshUnavailable):
                rotate_refresh_session(token.token, session, auth_settings)
        elif length >= 4095:
            with pytest.raises(RefreshLimitReached) as caught:
                rotate_refresh_session(token.token, session, auth_settings)
            assert caught.value.clear_cookies is True
        else:
            result = rotate_refresh_session(token.token, session, auth_settings)
            assert result.refresh.expires_at == token.expires_at
    with factory() as reader:
        rows = list(reader.scalars(select(RefreshSession).where(RefreshSession.id != other_id)))
        assert len(rows) == (length + 1 if length == 4094 else length)
        assert reader.get(RefreshSession, other_id).revoked_at is None
        if length in (4095, 4096):
            assert all(row.revoked_at is not None for row in rows)
        elif length == 4097:
            assert reader.get(RefreshSession, ids[-1]).revoked_at is None
