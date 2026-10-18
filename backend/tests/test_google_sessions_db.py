"""Google refresh persistence, atomic rollback, concurrency and compensation in guarded PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from fastapi import Response
import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.api.cookies import set_auth_cookies
from app.models.auth_identity import AuthIdentity
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.repositories.auth_identities import find_google_identity
from app.repositories.users import add_local_user
from app.security.google_identity import VerifiedGoogleIdentity
from app.security.session_values import create_csrf_token, create_refresh_digest
from app.security.tokens import TokenIssueError
from app.services.google_sessions import cancel_google_login, create_google_login_session
from app.services.login import LoginUnavailable

pytestmark = pytest.mark.db


@pytest.fixture
def identity():
    return VerifiedGoogleIdentity(
        issuer="https://accounts.google.com", subject=uuid4().hex, email=f"google-{uuid4().hex}@example.test"
    )


def counts(session):
    return tuple(
        session.scalar(select(func.count()).select_from(model)) for model in (User, AuthIdentity, RefreshSession)
    )


def test_first_and_repeat_login_store_independent_refresh_roots(committed_google_sessions, identity, auth_settings):
    results = []
    for _ in range(2):
        with committed_google_sessions() as session:
            result = create_google_login_session(identity, session, auth_settings)
            assert not session.in_transaction()
            results.append(result)
        # Cookie creation is possible only after the service has committed.
        response = Response()
        csrf = set_auth_cookies(response, result.refresh, auth_settings)
        assert csrf == create_csrf_token(result.refresh.jti, auth_settings)
        assert len(response.headers.getlist("set-cookie")) == 2
        assert not hasattr(result, "access")
        claims = jwt.decode(
            result.refresh.token,
            auth_settings.refresh_key,
            algorithms=["HS256"],
            issuer=auth_settings.issuer,
            audience=auth_settings.audience,
        )
        assert claims["sub"] == result.user.id and claims["token_type"] == "refresh"
        assert claims["exp"] - claims["iat"] == 14 * 24 * 60 * 60
        with committed_google_sessions() as reader:
            root = reader.scalar(
                select(RefreshSession).where(
                    RefreshSession.token_digest == create_refresh_digest(result.refresh.token, auth_settings)
                )
            )
            assert root.user_id == int(result.user.id) and len(root.token_digest) == 32
            assert root.rotated_from_id is None and root.revoked_at is None
            assert root.issued_at == result.refresh.issued_at
            assert root.expires_at == result.refresh.expires_at
            assert root.expires_at - root.issued_at == timedelta(days=14)
            assert reader.get(User, root.user_id).password_hash is None
    assert results[0].user.id == results[1].user.id
    assert results[0].refresh.jti != results[1].refresh.jti
    assert results[0].refresh.token != results[1].refresh.token
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 1, 2)


def test_same_email_local_user_never_receives_google_session(committed_google_sessions, identity, auth_settings):
    with committed_google_sessions() as session:
        local = add_local_user(session, identity.email, "fixture-local-hash")
        local_id = local.id
        session.commit()
        result = create_google_login_session(identity, session, auth_settings)
    assert int(result.user.id) != local_id
    with committed_google_sessions() as reader:
        assert reader.scalar(select(RefreshSession.user_id)) == int(result.user.id)
        assert reader.get(User, local_id).password_hash == "fixture-local-hash"
        assert counts(reader) == (2, 1, 1)


@pytest.mark.parametrize(
    "existing,stage",
    [(False, "user_insert"), (False, "google_identity_insert")]
    + [(existing, stage) for existing in (False, True) for stage in ("token_issue", "refresh_session", "commit")],
)
def test_storage_and_token_failure_leave_no_partial_login(
    committed_google_sessions, identity, auth_settings, existing, stage
):
    with committed_google_sessions() as session:
        if existing:
            first = create_google_login_session(identity, session, auth_settings)
            first_id = int(first.user.id)
        if stage == "token_issue":
            target = "app.services.google_sessions.issue_refresh_token"
            failure = TokenIssueError("fixture-private")
        else:
            target = {
                "user_insert": "app.services.google_accounts.add_google_user",
                "google_identity_insert": "app.services.google_accounts.add_google_identity",
                "refresh_session": "app.services.google_sessions.add_initial_refresh_session",
                "commit": "unused",
            }[stage]
            failure = OperationalError("fixture-private-SQL", None, RuntimeError("fixture-private"))

        def fail_commit():
            session.flush()
            raise failure

        failure_patch = (
            patch.object(session, "commit", side_effect=fail_commit)
            if stage == "commit"
            else patch(target, side_effect=failure)
        )
        with failure_patch, pytest.raises(LoginUnavailable) as caught:
            create_google_login_session(identity, session, auth_settings)
        assert caught.value.failure.stage == stage
        assert "fixture-private" not in str(caught.value)
        assert not session.in_transaction()
    with committed_google_sessions() as reader:
        assert counts(reader) == ((1, 1, 1) if existing else (0, 0, 0))
        if existing:
            assert reader.get(User, first_id) is not None


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_logins_share_one_account_and_create_independent_roots(
    committed_google_sessions, identity, auth_settings, existing
):
    if existing:
        with committed_google_sessions() as session:
            create_google_login_session(identity, session, auth_settings)
    barrier = Barrier(2)

    def synchronized_lookup(session, issuer, subject):
        result = find_google_identity(session, issuer, subject)
        if not session.info.get("initial_lookup_done"):
            session.info["initial_lookup_done"] = True
            barrier.wait(timeout=10)
        return result

    def login():
        with committed_google_sessions() as session:
            return create_google_login_session(identity, session, auth_settings)

    with (
        patch("app.services.google_accounts.find_google_identity", side_effect=synchronized_lookup),
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        futures = [workers.submit(login) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0].user.id == results[1].user.id
    assert results[0].refresh.jti != results[1].refresh.jti
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 1, 3 if existing else 2)


def test_response_failure_cancels_only_its_root_and_preserves_adopted_account(
    committed_google_sessions, identity, auth_settings
):
    with committed_google_sessions() as session:
        failed = create_google_login_session(identity, session, auth_settings)
    with committed_google_sessions() as other:
        successful = create_google_login_session(identity, other, auth_settings)
    with committed_google_sessions() as session:
        cancel_google_login(failed, session, auth_settings)
        cancel_google_login(failed, session, auth_settings)
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 1, 1)
        assert reader.scalar(select(RefreshSession.token_digest)) == create_refresh_digest(
            successful.refresh.token, auth_settings
        )
        assert find_google_identity(reader, identity.issuer, identity.subject).user_id == int(successful.user.id)
    with committed_google_sessions() as session:
        retry = create_google_login_session(identity, session, auth_settings)
        assert retry.user.id == failed.user.id


def test_cancelled_first_login_preserves_identity_but_removes_unusable_refresh(
    committed_google_sessions, identity, auth_settings
):
    with committed_google_sessions() as session:
        result = create_google_login_session(identity, session, auth_settings)
        cancel_google_login(result, session, auth_settings)
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 1, 0)


def test_changed_refresh_is_not_deleted_by_compensation(committed_google_sessions, identity, auth_settings):
    with committed_google_sessions() as session:
        result = create_google_login_session(identity, session, auth_settings)
        root = session.scalar(select(RefreshSession))
        root.revoked_at = result.refresh.issued_at
        session.commit()
        with pytest.raises(LoginUnavailable) as caught:
            cancel_google_login(result, session, auth_settings)
        assert caught.value.failure.error_type == "LoginStateChanged"
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 1, 1)
        assert reader.scalar(select(RefreshSession.revoked_at)) is not None


def test_failed_compensation_commit_rolls_back_deletion(committed_google_sessions, identity, auth_settings):
    with committed_google_sessions() as session:
        result = create_google_login_session(identity, session, auth_settings)
        with patch.object(session, "commit", side_effect=OperationalError("private", None, RuntimeError("private"))):
            with pytest.raises(LoginUnavailable) as caught:
                cancel_google_login(result, session, auth_settings)
        assert caught.value.failure.stage == "compensation"
        assert "private" not in str(caught.value)
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 1, 1)
