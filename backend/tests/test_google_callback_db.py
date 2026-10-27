"""End-to-end Google callback HTTP with real RSA and guarded PostgreSQL persistence."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
import json
import logging
import secrets
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
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
from app.security.google_transactions import encode_google_transaction
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


def assert_failure(response, reason="temporarily_unavailable", *, fallback=False):
    if fallback:
        assert response.status_code == 503 and "location" not in response.headers
        assert response.json()["error"]["trace_id"]
    else:
        assert response.status_code == 302
        assert response.headers["location"] == "/?google_login_error=" + reason
        assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    values = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        values.load(header)
    assert set(values) == {GOOGLE_TRANSACTION_COOKIE_NAME}
    cookie = values[GOOGLE_TRANSACTION_COOKIE_NAME]
    assert cookie["max-age"] == "0" and cookie["path"] == "/api/auth"
    assert cookie["httponly"] and cookie["samesite"] == "lax" and not cookie["domain"]


def replace_transaction_cookie(flow, value):
    # Mutate the actual host-only cookie; an unscoped Cookies.set creates a different cookie identity.
    cookie = next(item for item in flow.client.cookies.jar if item.name == GOOGLE_TRANSACTION_COOKIE_NAME)
    assert cookie.domain == "127.0.0.1" and cookie.path == "/api/auth" and not cookie.domain_specified
    cookie.value = value


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
    reason = "invalid_request"
    if failure == "cancelled":
        params.pop("code")
        params["error"] = "access_denied"
        reason = "cancelled"
    elif failure == "claim":
        flow.behavior["claims"] = {"email_verified": False}
    elif failure == "provider":
        flow.behavior["discovery_failure"] = True
        reason = "temporarily_unavailable"
    else:
        flow.behavior["exchange_error"] = "invalid_grant"
    assert_failure(flow.client.get(PATH, params=params), reason)
    assert counts(flow) == (0, 0, 0)


def test_replayed_callback_with_restored_transaction_preserves_existing_session(callback_db, auth_settings):
    flow = callback_db
    first = flow.client.get(PATH, params=flow.params)
    assert first.status_code == 302
    original = first.cookies["refresh_token"]
    response = flow.client.get(
        PATH, params=flow.params, headers={"Cookie": f"google_transaction={flow.cookie}; refresh_token={original}"}
    )
    assert_failure(response, "invalid_request")
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
    assert sorted(response.headers["location"] for response in responses) == [
        "/",
        "/?google_login_error=invalid_request",
    ]
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
    assert_failure(response)
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
    assert_failure(response)
    assert counts(flow) == (1, 1, int(other_login))
    assert "event=login_cancelled" in caplog.text and "fixture-private" not in caplog.text
    flow.start(code="fixture-retry-code")
    assert flow.client.get(PATH, params=flow.params).status_code == 302
    assert counts(flow) == (1, 1, int(other_login) + 1)


def test_response_construction_failure_also_cancels_committed_refresh(callback_db):
    with patch("app.api.google_auth.RedirectResponse", side_effect=RuntimeError("fixture-private-response")):
        response = callback_db.client.get(PATH, params=callback_db.params)
    assert_failure(response, fallback=True)
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
    assert_failure(response)
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
    assert_failure(response)
    assert counts(callback_db) == (1, 1, 1)
    assert "event=login_compensation_failed" in caplog.text
    assert "event=login_cancelled" not in caplog.text and "fixture-private" not in caplog.text


@pytest.mark.parametrize(
    "kind",
    [
        "missing-code",
        "missing-cookie",
        "tampered-cookie",
        "expired-cookie",
        "wrong-state",
        "duplicate-state",
        "code-and-error",
        "wrong-issuer",
    ],
)
def test_invalid_callbacks_never_reach_google_or_persist_rows(callback_db, kind, caplog):
    flow = callback_db
    params = dict(flow.params)
    if kind == "missing-code":
        params.pop("code")
    elif kind == "missing-cookie":
        flow.client.cookies.clear()
    elif kind == "tampered-cookie":
        replace_transaction_cookie(flow, "fixture-private-cookie")
    elif kind == "expired-cookie":
        now = datetime.now(timezone.utc) - timedelta(seconds=301)
        old = replace(flow.transaction, issued_at=int(now.timestamp()), expires_at=int(now.timestamp()) + 300)
        replace_transaction_cookie(flow, encode_google_transaction(old, flow.settings, now=now))
    elif kind == "wrong-state":
        params["state"] = "fixture-private-state"
    elif kind == "duplicate-state":
        params = [*params.items(), ("state", "fixture-private-state")]
    elif kind == "code-and-error":
        params["error"] = "access_denied"
    else:
        params["iss"] = "https://evil.example.test"
    with caplog.at_level(logging.INFO):
        response = flow.client.get(PATH, params=params)
    assert_failure(response, "invalid_request")
    assert counts(flow) == (0, 0, 0)
    assert flow.calls == []
    assert GOOGLE_TRANSACTION_COOKIE_NAME not in flow.client.cookies
    assert "fixture-private" not in caplog.text + str(response.headers) + response.text


@pytest.mark.parametrize("return_to", ["//evil.test", "https://evil.test", "/%2fevil.test", "/\\evil.test"])
def test_even_authenticated_cookie_cannot_expand_return_to_allowlist(callback_db, return_to):
    flow = callback_db
    # Create a valid AES-GCM envelope with an invalid destination to exercise validation after decryption.
    encoded = flow.cookie.split(".", 1)[1]
    raw = urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    context = json.dumps(
        ["google-login-transaction", "v1", flow.settings.client_id, flow.settings.redirect_uri], separators=(",", ":")
    ).encode()
    cipher = AESGCM(flow.settings.transaction_key)
    payload = json.loads(cipher.decrypt(raw[:12], raw[12:], context))
    payload["return_to"] = return_to
    nonce = secrets.token_bytes(12)
    forged = (
        "v1."
        + urlsafe_b64encode(nonce + cipher.encrypt(nonce, json.dumps(payload).encode(), context)).rstrip(b"=").decode()
    )
    replace_transaction_cookie(flow, forged)
    response = flow.client.get(PATH, params={**flow.params, "return_to": return_to})
    assert_failure(response, "invalid_request")
    assert flow.calls == []
    assert counts(flow) == (0, 0, 0)
    assert "evil.test" not in str(response.headers)


@pytest.mark.parametrize("failure", ["cancelled", "invalid_request", "temporarily_unavailable"])
def test_failure_keeps_existing_session_usable_through_refresh_and_me(callback_db, auth_settings, failure, caplog):
    flow = callback_db
    first = flow.client.get(PATH, params=flow.params)
    original_refresh = first.cookies["refresh_token"]
    original_csrf = first.cookies["csrf_token"]
    original_digest = create_refresh_digest(original_refresh, auth_settings)
    with flow.sessions() as reader:
        user_id = str(reader.scalar(select(RefreshSession.user_id)))
    flow.start(code="fixture-second-login-code")
    params = dict(flow.params)
    if failure == "cancelled":
        params.pop("code")
        params["error"] = "access_denied"
    elif failure == "invalid_request":
        params["state"] = "fixture-private-state"
    else:
        flow.behavior["discovery_failure"] = True
    with caplog.at_level(logging.INFO):
        response = flow.client.get(PATH, params=params)
    assert_failure(response, failure)
    assert flow.client.cookies["refresh_token"] == original_refresh
    assert flow.client.cookies["csrf_token"] == original_csrf
    assert counts(flow) == (1, 1, 1)
    with flow.sessions() as reader:
        stored = reader.scalar(select(RefreshSession))
        assert stored.token_digest == original_digest and stored.revoked_at is None
    csrf = flow.client.get("/api/auth/csrf")
    assert csrf.status_code == 200
    refreshed = flow.client.post(
        "/api/auth/refresh",
        headers={"Origin": "http://127.0.0.1:5173", "X-CSRF-Token": csrf.json()["csrf_token"]},
    )
    assert refreshed.status_code == 200
    access = refreshed.json()["access_token"]
    me = flow.client.get("/api/me", headers={"Authorization": "Bearer " + access})
    assert me.status_code == 200 and me.json()["user"]["id"] == user_id
    for secret in (original_refresh, original_csrf, access, flow.cookie, flow.transaction.state):
        assert secret not in caplog.text + response.text + str(response.headers)


@pytest.mark.parametrize("error,reason", [("access_denied", "cancelled"), ("server_error", "temporarily_unavailable")])
def test_failed_attempt_can_restart_and_create_exactly_one_session(callback_db, error, reason):
    flow = callback_db
    old = flow.transaction
    failed = flow.client.get(PATH, params={"state": old.state, "error": error})
    assert_failure(failed, reason)
    assert counts(flow) == (0, 0, 0)
    assert GOOGLE_TRANSACTION_COOKIE_NAME not in flow.client.cookies
    flow.start(code="fixture-retry-after-failure-code")
    assert flow.transaction.state != old.state and flow.transaction.code_verifier != old.code_verifier
    assert flow.transaction.nonce != old.nonce
    success = flow.client.get(PATH, params=flow.params)
    assert success.status_code == 302 and success.headers["location"] == "/"
    assert counts(flow) == (1, 1, 1)
    assert GOOGLE_TRANSACTION_COOKIE_NAME not in flow.client.cookies


@pytest.mark.parametrize("failure", ["nonce", "audience", "expired-token", "signature", "pkce", "jwks"])
def test_oidc_guards_remain_enforced_before_session_creation(callback_db, failure, caplog):
    flow = callback_db
    reason = "invalid_request"
    if failure == "nonce":
        flow.behavior["claims"] = {"nonce": "fixture-private-nonce"}
    elif failure == "audience":
        flow.behavior["claims"] = {"aud": "fixture-private-client"}
    elif failure == "expired-token":
        flow.behavior["claims"] = {"iat": 1, "exp": 2}
    elif failure == "signature":
        flow.behavior["signing_key"] = generate_private_key(public_exponent=65537, key_size=2048)
    elif failure == "pkce":
        wrong = replace(flow.transaction, code_verifier="x" * 43)
        replace_transaction_cookie(flow, encode_google_transaction(wrong, flow.settings))
    else:
        flow.behavior["jwks_failure"] = True
        reason = "temporarily_unavailable"
    with caplog.at_level(logging.INFO):
        response = flow.client.get(PATH, params=flow.params)
    assert_failure(response, reason)
    assert counts(flow) == (0, 0, 0)
    assert sum(request.method == "POST" for request in flow.calls) == 1
    for secret in (flow.params["code"], flow.transaction.state, flow.cookie, "fixture-private"):
        assert secret not in caplog.text + response.text + str(response.headers)
