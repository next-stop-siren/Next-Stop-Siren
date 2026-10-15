"""Google account isolation, savepoints and concurrent creation in guarded PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models.auth_identity import AuthIdentity
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.repositories.auth_identities import add_google_identity, find_google_identity
from app.repositories.users import add_local_user, find_local_user_by_email
from app.security.google_identity import VerifiedGoogleIdentity
from app.services.google_accounts import GoogleAccountUnavailable, get_or_create_google_user

pytestmark = pytest.mark.db


@pytest.fixture
def identity():
    return VerifiedGoogleIdentity(
        issuer="https://accounts.google.com",
        subject=uuid4().hex,
        email=f"google-{uuid4().hex}@example.test",
    )


def counts(session):
    return tuple(
        session.scalar(select(func.count()).select_from(model)) for model in (User, AuthIdentity, RefreshSession)
    )


def test_first_login_adds_user_and_identity_without_committing_or_issuing_tokens(committed_google_sessions, identity):
    with committed_google_sessions() as session:
        result = get_or_create_google_user(identity, session)
        assert result.created is True and result.user.password_hash is None
        assert result.user.email == identity.email and result.user.created_at is not None
        assert counts(session) == (1, 1, 0)
        with committed_google_sessions() as reader:
            assert counts(reader) == (0, 0, 0)
        user_id = result.user.id
        session.commit()
    with committed_google_sessions() as reader:
        stored = find_google_identity(reader, identity.issuer, identity.subject)
        assert stored.user_id == user_id and stored.provider == "google"
        assert reader.get(User, user_id).password_hash is None
        assert counts(reader) == (1, 1, 0)


def test_repeat_login_uses_same_id_even_when_google_email_changes(committed_google_sessions, identity):
    with committed_google_sessions() as session:
        first = get_or_create_google_user(identity, session)
        user_id = first.user.id
        session.commit()
    with committed_google_sessions() as session:
        second = get_or_create_google_user(replace(identity, email="changed@example.test"), session)
        assert second.created is False and second.user.id == user_id
        assert second.user.email == identity.email
        assert counts(session) == (1, 1, 0)
        session.commit()


@pytest.mark.parametrize("local_first", [True, False])
def test_google_and_local_users_with_same_email_remain_separate(committed_google_sessions, identity, local_first):
    with committed_google_sessions() as session:
        if local_first:
            local = add_local_user(session, identity.email.upper(), "fixture-local-hash")
        google = get_or_create_google_user(identity, session)
        if not local_first:
            local = add_local_user(session, identity.email.upper(), "fixture-local-hash")
        assert local.id != google.user.id
        assert local.password_hash == "fixture-local-hash" and google.user.password_hash is None
        assert find_local_user_by_email(session, identity.email).id == local.id
        assert find_google_identity(session, identity.issuer, identity.subject).user_id == google.user.id
        assert counts(session) == (2, 1, 0)
        session.commit()


def test_same_email_different_subjects_get_different_users(committed_google_sessions, identity):
    with committed_google_sessions() as session:
        first = get_or_create_google_user(identity, session)
        second = get_or_create_google_user(replace(identity, subject=uuid4().hex), session)
        assert first.user.id != second.user.id
        assert counts(session) == (2, 2, 0)
        session.commit()


def test_unlinked_user_with_same_email_is_not_adopted(committed_google_sessions, identity):
    with committed_google_sessions() as session:
        unlinked = User(email=identity.email, password_hash=None)
        session.add(unlinked)
        session.flush()
        result = get_or_create_google_user(identity, session)
        assert result.user.id != unlinked.id and result.created is True
        assert counts(session) == (2, 1, 0)
        session.commit()


def test_case_sensitive_subjects_are_distinct(committed_google_sessions, identity):
    with committed_google_sessions() as session:
        first = get_or_create_google_user(replace(identity, subject="CaseSensitive"), session)
        second = get_or_create_google_user(replace(identity, subject="casesensitive"), session)
        assert first.user.id != second.user.id
        session.commit()


def test_identity_constraint_failure_rolls_back_its_new_user_and_preserves_prior_work(
    committed_google_sessions, identity
):
    with committed_google_sessions() as session:
        prior = add_local_user(session, "prior@example.test", "fixture-local-hash")
        prior_id = prior.id
        with patch(
            "app.services.google_accounts.add_google_identity",
            side_effect=lambda session, user_id, issuer, subject: add_google_identity(session, user_id, issuer, ""),
        ):
            with pytest.raises(GoogleAccountUnavailable) as caught:
                get_or_create_google_user(identity, session)
        assert caught.value.failure.stage == "google_identity_insert"
        assert counts(session) == (1, 0, 0)
        assert session.get(User, prior_id) is not None
        session.commit()
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 0, 0)


def test_missed_precheck_recovers_only_identity_unique_conflict(committed_google_sessions, identity):
    with committed_google_sessions() as session:
        first = get_or_create_google_user(identity, session)
        user_id = first.user.id
        session.commit()
    calls = 0

    def miss_once(session, issuer, subject):
        nonlocal calls
        calls += 1
        return None if calls == 1 else find_google_identity(session, issuer, subject)

    with (
        committed_google_sessions() as session,
        patch("app.services.google_accounts.find_google_identity", side_effect=miss_once),
    ):
        result = get_or_create_google_user(identity, session)
        assert result.created is False and result.user.id == user_id
        assert counts(session) == (1, 1, 0)
        session.commit()


def test_caller_rollback_removes_both_new_rows(committed_google_sessions, identity):
    with committed_google_sessions() as session:
        get_or_create_google_user(identity, session)
        session.rollback()
    with committed_google_sessions() as reader:
        assert counts(reader) == (0, 0, 0)


def test_commit_failure_does_not_leave_partial_data_after_caller_rollback(committed_google_sessions, identity):
    with committed_google_sessions() as session:
        get_or_create_google_user(identity, session)
        with patch.object(
            session, "commit", side_effect=OperationalError("fixture-private-SQL", None, RuntimeError("private"))
        ):
            with pytest.raises(OperationalError):
                session.commit()
        session.rollback()
    with committed_google_sessions() as reader:
        assert counts(reader) == (0, 0, 0)


@pytest.mark.parametrize("same_email", [True, False])
def test_concurrent_first_logins_commit_one_identity_without_orphan_users(
    committed_google_sessions, identity, same_email
):
    barrier = Barrier(2)

    def synchronized_lookup(session, issuer, subject):
        found = find_google_identity(session, issuer, subject)
        if not session.info.get("initial_lookup_done"):
            session.info["initial_lookup_done"] = True
            barrier.wait(timeout=10)
        return found

    def resolve(candidate):
        with committed_google_sessions() as session:
            result = get_or_create_google_user(candidate, session)
            snapshot = (result.user.id, result.created)
            # Commit immediately: the competing request may be waiting for this transaction.
            session.commit()
            return snapshot

    other = identity if same_email else replace(identity, email="other@example.test")
    with (
        patch("app.services.google_accounts.find_google_identity", side_effect=synchronized_lookup),
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = [
            future.result(timeout=20)
            for future in [workers.submit(resolve, candidate) for candidate in (identity, other)]
        ]
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 1, 0)
        assert find_google_identity(reader, identity.issuer, identity.subject).user_id == results[0][0]


def test_rolled_back_competitor_does_not_leave_user_or_steal_identity(committed_google_sessions, identity):
    inserted = Event()
    competitor_started = Event()

    def coordinated_insert(session, user_id, issuer, subject):
        if session.info["role"] == "rollback":
            result = add_google_identity(session, user_id, issuer, subject)
            inserted.set()
            assert competitor_started.wait(timeout=10)
            return result
        assert inserted.wait(timeout=10)
        competitor_started.set()
        return add_google_identity(session, user_id, issuer, subject)

    def resolve(role):
        with committed_google_sessions() as session:
            session.info["role"] = role
            result = get_or_create_google_user(identity, session)
            user_id = result.user.id
            if role == "rollback":
                session.rollback()
            else:
                session.commit()
            return user_id

    with (
        patch("app.services.google_accounts.add_google_identity", side_effect=coordinated_insert),
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        abandoned = workers.submit(resolve, "rollback")
        assert inserted.wait(timeout=10)
        winner = workers.submit(resolve, "commit")
        abandoned_id, winner_id = abandoned.result(timeout=20), winner.result(timeout=20)
    assert abandoned_id != winner_id
    with committed_google_sessions() as reader:
        assert counts(reader) == (1, 1, 0)
        assert reader.get(User, abandoned_id) is None
        assert find_google_identity(reader, identity.issuer, identity.subject).user_id == winner_id
