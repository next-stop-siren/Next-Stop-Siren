"""End-to-end Google callback HTTP with real RSA and guarded PostgreSQL persistence."""

from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
import logging
from unittest.mock import patch

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError
from app.api.google_cookies import GOOGLE_TRANSACTION_COOKIE_NAME, clear_google_transaction_cookie
from app.models.auth_identity import AuthIdentity
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.repositories.users import add_local_user
from app.security.google_identity import VerifiedGoogleIdentity
from app.security.session_values import create_csrf_token, create_refresh_digest
from app.security.tokens import TokenIssueError
from app.services.google_sessions import create_google_login_session

pytestmark = pytest.mark.db
PATH = "/api/auth/google/callback"


@pytest.fixture
def callback_db(google_callback_flow, committed_google_sessions):
    with patch("app.db._session_factory", committed_google_sessions):
        google_callback_flow.sessions = committed_google_sessions
        yield google_callback_flow


def counts(flow):
    with flow.sessions() as reader:
        return tuple(
            reader.scalar(select(func.count()).select_from(model)) for model in (User, AuthIdentity, RefreshSession)
        )


def assert_failure(response, status):
    assert response.status_code == status and "location" not in response.headers
    assert response.json()["error"]["trace_id"]
    assert response.headers["cache-control"] == "no-store"
    values = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        values.load(header)
    assert set(values) == {GOOGLE_TRANSACTION_COOKIE_NAME}
    assert values[GOOGLE_TRANSACTION_COOKIE_NAME]["max-age"] == "0"


def test_first_and_repeat_callback_commit_before_cookies_and_keep_local_account_separate(callback_db, auth_settings):
    flow = callback_db
    with flow.sessions() as session:
        local = add_local_user(session, "fixture@example.test", "fixture-local-hash")
        local_id = local.id
        session.commit()
    user_ids, jtis = [], []
    for index in range(2):
        if index:
            flow.start(code="fixture-fresh-code")
        response = flow.client.get(PATH, params=flow.params)
        assert response.status_code == 302 and response.headers["location"] == "/"
        assert response.content == b""
        refresh = response.cookies["refresh_token"]
        claims = jwt.decode(
            refresh,
            auth_settings.refresh_key,
            algorithms=["HS256"],
            issuer=auth_settings.issuer,
            audience=auth_settings.audience,
        )
        assert claims["token_type"] == "refresh" and claims["exp"] - claims["iat"] == 14 * 24 * 3600
        assert response.cookies["csrf_token"] == create_csrf_token(claims["jti"], auth_settings)
        user_ids.append(int(claims["sub"]))
        jtis.append(claims["jti"])
        with flow.sessions() as reader:
            stored = reader.scalar(
                select(RefreshSession).where(
                    RefreshSession.token_digest == create_refresh_digest(refresh, auth_settings)
                )
            )
            assert stored.user_id == user_ids[-1] and stored.rotated_from_id is None and stored.revoked_at is None
            assert reader.get(User, user_ids[-1]).password_hash is None
            assert reader.get(User, local_id).password_hash == "fixture-local-hash"
        assert refresh not in response.text and "access" not in response.headers["location"]
    assert user_ids[0] == user_ids[1] != local_id and jtis[0] != jtis[1]
    assert counts(flow) == (2, 1, 2)


@pytest.mark.parametrize("failure", ["cancelled", "claim", "provider", "invalid-grant"])
def test_verification_failures_create_no_accounts_or_sessions(callback_db, failure):
    flow = callback_db
    params = dict(flow.params)
    status = 401
    if failure == "cancelled":
        params.pop("code")
        params["error"] = "access_denied"
    elif failure == "claim":
        flow.behavior["claims"] = {"email_verified": False}
    elif failure == "provider":
        flow.behavior["discovery_failure"] = True
        status = 503
    else:
        flow.behavior["exchange_error"] = "invalid_grant"
    assert_failure(flow.client.get(PATH, params=params), status)
    assert counts(flow) == (0, 0, 0)


def test_replayed_callback_with_restored_transaction_preserves_existing_session(callback_db, auth_settings):
    flow = callback_db
    first = flow.client.get(PATH, params=flow.params)
    assert first.status_code == 302
    original = first.cookies["refresh_token"]
    response = flow.client.get(
        PATH, params=flow.params, headers={"Cookie": f"google_transaction={flow.cookie}; refresh_token={original}"}
    )
    assert_failure(response, 401)
    assert counts(flow) == (1, 1, 1)
    assert flow.client.cookies.get("refresh_token") == original
    with flow.sessions() as reader:
        assert reader.scalar(select(RefreshSession.token_digest)) == create_refresh_digest(original, auth_settings)


def test_simultaneous_duplicate_callbacks_issue_exactly_one_session(callback_db):
    flow = callback_db

    def callback():
        return flow.client.get(PATH, params=flow.params, headers={"Cookie": f"google_transaction={flow.cookie}"})

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(callback) for _ in range(2)]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses) == [302, 401]
    assert counts(flow) == (1, 1, 1)


@pytest.mark.parametrize("stage", ["token", "commit"])
def test_save_failure_returns_no_auth_cookies_and_rolls_back_all_rows(callback_db, stage):
    def fail_commit(session):
        session.flush()
        raise OperationalError("fixture-private-SQL", None, RuntimeError("fixture-private"))

    failure = (
        patch.object(Session, "commit", fail_commit)
        if stage == "commit"
        else patch("app.services.google_sessions.issue_refresh_token", side_effect=TokenIssueError("fixture-private"))
    )
    with failure:
        response = callback_db.client.get(PATH, params=callback_db.params)
    assert_failure(response, 503)
    assert counts(callback_db) == (0, 0, 0)
    assert "fixture-private" not in response.text


@pytest.mark.parametrize("other_login", [False, True])
def test_cookie_failure_cancels_this_session_preserving_account_and_other_logins(
    callback_db, auth_settings, other_login, caplog
):
    flow = callback_db

    def partial_cookie(response, refresh, settings):
        if other_login:
            with flow.sessions() as session:
                create_google_login_session(
                    VerifiedGoogleIdentity(
                        issuer=flow.settings.issuer, subject="fixture-google-subject", email="fixture@example.test"
                    ),
                    session,
                    auth_settings,
                )
        response.set_cookie("refresh_token", refresh.token)
        raise AuthCookieError("fixture-private-cookie")

    with patch("app.api.google_auth.set_auth_cookies", side_effect=partial_cookie), caplog.at_level(logging.INFO):
        response = flow.client.get(PATH, params=flow.params)
    assert_failure(response, 503)
    assert counts(flow) == (1, 1, int(other_login))
    assert "event=login_cancelled" in caplog.text and "fixture-private" not in caplog.text
    flow.start(code="fixture-retry-code")
    assert flow.client.get(PATH, params=flow.params).status_code == 302
    assert counts(flow) == (1, 1, int(other_login) + 1)


def test_response_construction_failure_also_cancels_committed_refresh(callback_db):
    with patch("app.api.google_auth.RedirectResponse", side_effect=RuntimeError("fixture-private-response")):
        response = callback_db.client.get(PATH, params=callback_db.params)
    assert_failure(response, 503)
    assert counts(callback_db) == (1, 1, 0)


def test_transaction_cleanup_failure_discards_auth_cookies_and_compensates(callback_db):
    calls = 0

    def fail_once(response, settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fixture-private-cleanup")
        clear_google_transaction_cookie(response, settings)

    with patch("app.api.google_auth.clear_google_transaction_cookie", side_effect=fail_once):
        response = callback_db.client.get(PATH, params=callback_db.params)
    assert_failure(response, 503)
    assert counts(callback_db) == (1, 1, 0)


def test_compensation_failure_is_logged_without_issuing_partial_cookies(callback_db, caplog):
    original = Session.commit
    calls = 0

    def commit(session):
        nonlocal calls
        calls += 1
        if calls == 2:
            session.flush()
            raise OperationalError("fixture-private-DELETE", None, RuntimeError("fixture-private"))
        return original(session)

    with (
        patch.object(Session, "commit", commit),
        patch("app.api.google_auth.set_auth_cookies", side_effect=AuthCookieError("fixture-private-cookie")),
        caplog.at_level(logging.INFO),
    ):
        response = callback_db.client.get(PATH, params=callback_db.params)
    assert_failure(response, 503)
    assert counts(callback_db) == (1, 1, 1)
    assert "event=login_compensation_failed" in caplog.text
    assert "event=login_cancelled" not in caplog.text and "fixture-private" not in caplog.text
