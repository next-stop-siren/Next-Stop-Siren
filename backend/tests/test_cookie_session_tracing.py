"""Correlate cookie session requests and committed outcomes without logging credentials."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError

from app.api.cookies import AuthCookieError
from app.models.refresh_session import RefreshSession
from app.observability import FailureDetails
from app.security.session_values import create_refresh_digest
from app.services.csrf import read_csrf_token
from app.services.logout import logout_refresh_session
from app.services.refresh import RefreshRace, RefreshUnavailable, rotate_refresh_session
from app.services.refresh_sessions import LockedRefreshLineage
from test_cookie_session_api import api as api

OUTCOMES = {
    "csrf": "csrf_read",
    "rotate": "refresh_rotated",
    "reuse": "refresh_lineage_revoked",
    "limit": "refresh_lineage_revoked",
    "logout": "logout_saved",
}


def events(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("app.") and "event=" in record.getMessage()
    ]


def assert_correlated(caplog, api, response):
    records = events(caplog)
    ids = [message.split("trace_id=", 1)[1].split()[0] for message in records]
    assert len(set(ids)) == 1 and str(UUID(ids[0])) == ids[0]
    if response.status_code >= 400:
        assert response.json()["error"]["trace_id"] == ids[0]
    secrets = [
        api.original.token,
        api.result.refresh.token,
        api.result.access.token,
        api.headers["X-CSRF-Token"],
        str(create_refresh_digest(api.original.token, api.settings)),
        create_refresh_digest(api.original.token, api.settings).hex(),
        "fixture-client-trace",
        "private SQL",
        "private cookie",
    ]
    if response.status_code == 200:
        body = response.json()
        secrets.extend(body[key] for key in ("access_token", "csrf_token") if key in body)
    assert all(secret not in caplog.text for secret in secrets)
    return [message.split()[0].removeprefix("event=") for message in records]


def request(api, operation, *, headers=None):
    path = operation if operation in {"csrf", "logout"} else "refresh"
    combined = {**api.headers, "X-Trace-ID": "fixture-client-trace", **(headers or {})}
    if path == "csrf":
        return api.client.get(f"/api/auth/{path}", headers=combined)
    return api.client.post(f"/api/auth/{path}", headers=combined)


@pytest.fixture
def real_service(api):
    """Use actual commit/rollback paths; only persistence lookups and the Session are mocked."""
    original = api.original
    root = RefreshSession(
        id=1,
        user_id=101,
        token_digest=create_refresh_digest(original.token, api.settings),
        issued_at=original.issued_at,
        expires_at=original.expires_at,
        rotated_from_id=None,
        revoked_at=None,
    )
    locked = LockedRefreshLineage(current=root, sessions=(root,))
    api.read.side_effect = read_csrf_token
    api.rotate.side_effect = rotate_refresh_session
    api.logout.side_effect = logout_refresh_session
    with (
        patch("app.services.csrf.lock_refresh_lineage", return_value=locked),
        patch("app.services.refresh.lock_refresh_lineage", return_value=locked),
        patch("app.services.logout.lock_refresh_lineage", return_value=locked),
    ):
        yield locked


@pytest.mark.parametrize("operation", list(OUTCOMES))
@pytest.mark.parametrize("commit_failure", [False, True])
def test_outcome_is_logged_only_after_real_service_commit(api, real_service, caplog, operation, commit_failure):
    caplog.set_level(logging.INFO)
    root = real_service.current
    if operation == "reuse":
        root.revoked_at = datetime.now(timezone.utc) - timedelta(seconds=6)
        child = RefreshSession(
            id=2,
            user_id=101,
            issued_at=root.issued_at + timedelta(seconds=1),
            expires_at=root.expires_at,
            rotated_from_id=root.id,
            revoked_at=None,
        )
        real_service = replace(real_service, sessions=(root, child))

    def commit():
        # Before commit returns, no saved outcome is permitted.
        assert all(not any(f"event={value} " in message for value in OUTCOMES.values()) for message in events(caplog))
        if commit_failure:
            raise OperationalError("private SQL", None, None)

    api.session.commit.side_effect = commit
    # CSRF retrieval releases its read transaction with rollback rather than commit.
    if operation == "csrf" and commit_failure:
        api.session.rollback.side_effect = OperationalError("private SQL", None, None)
    with (
        patch("app.services.refresh.MAX_REFRESH_LINEAGE_ISSUANCES", 1 if operation == "limit" else 4095),
        patch("app.services.refresh.lock_refresh_lineage", return_value=real_service),
    ):
        response = request(api, operation)
    names = assert_correlated(caplog, api, response)
    received = f"{operation if operation in {'csrf', 'logout'} else 'refresh'}_received"
    assert names[0] == received
    if commit_failure:
        assert response.status_code == 503
        assert names == [received, f"{received.removesuffix('_received')}_failed"]
    else:
        assert response.status_code == (
            401 if operation in {"reuse", "limit"} else 204 if operation == "logout" else 200
        )
        assert names[1] == OUTCOMES[operation]
        if operation in {"reuse", "limit"}:
            assert f"reason={operation}" in events(caplog)[1]
        if operation == "csrf":
            api.session.commit.assert_not_called()
        else:
            api.session.commit.assert_called_once_with()


@pytest.mark.parametrize(
    "operation,failure",
    [("csrf", "token"), *[(op, failure) for op in ("rotate", "logout") for failure in ("token", "csrf", "origin")]],
)
def test_guard_rejection_is_traced_before_opening_db(api, caplog, operation, failure):
    caplog.set_level(logging.INFO)
    if failure == "token":
        api.client.cookies.clear()
    headers = (
        {"X-CSRF-Token": "wrong"}
        if failure == "csrf"
        else {"Origin": "http://invalid.example"}
        if failure == "origin"
        else {}
    )
    response = request(api, operation, headers=headers)
    names = assert_correlated(caplog, api, response)
    prefix = operation if operation != "rotate" else "refresh"
    assert response.status_code == (401 if failure == "token" else 403)
    assert names == [f"{prefix}_received", f"{prefix}_rejected"]
    api.opened.assert_not_called()


@pytest.mark.parametrize("cancel_failure", [False, True])
def test_response_failure_links_saved_rotation_compensation_and_failure(api, caplog, cancel_failure):
    caplog.set_level(logging.INFO)
    if cancel_failure:
        api.cancel.side_effect = RefreshUnavailable(
            failure=FailureDetails(stage="compensation", error_type="OperationalError")
        )
    with patch("app.api.cookie_sessions.set_auth_cookies", side_effect=AuthCookieError("private cookie")):
        response = request(api, "rotate")
    assert response.status_code == 503
    assert assert_correlated(caplog, api, response) == [
        "refresh_received",
        "refresh_rotated",
        "refresh_compensation_failed" if cancel_failure else "refresh_cancelled",
        "refresh_failed",
    ]


def test_race_has_correlated_rejection_without_saved_outcome(api, caplog):
    caplog.set_level(logging.INFO)
    api.rotate.side_effect = RefreshRace()
    response = request(api, "rotate")
    assert response.status_code == 409 and "set-cookie" not in response.headers
    assert assert_correlated(caplog, api, response) == ["refresh_received", "refresh_rejected"]


def test_separate_requests_have_distinct_server_ids(api, caplog):
    caplog.set_level(logging.INFO)
    ids = []
    for _ in range(2):
        caplog.clear()
        response = request(api, "csrf")
        assert assert_correlated(caplog, api, response) == ["csrf_received", "csrf_read"]
        ids.append(events(caplog)[0].split("trace_id=", 1)[1])
    assert ids[0] != ids[1]
