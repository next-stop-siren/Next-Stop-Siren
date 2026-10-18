"""Resolve verified Google identities without linking accounts by email."""

from dataclasses import dataclass, field

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.auth_identity import AuthIdentity
from app.models.user import User
from app.observability import FailureDetails, FailureStage
from app.repositories.auth_identities import add_google_identity, find_google_identity
from app.repositories.users import add_google_user, find_user_for_update
from app.security.google_identity import VerifiedGoogleIdentity


class GoogleAccountUnavailable(Exception):
    """Hide SQL, email, and identity values in account persistence failures."""

    def __init__(self, *, failure: FailureDetails) -> None:
        super().__init__("Google 계정을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.")
        self.failure = failure


@dataclass(frozen=True, slots=True)
class GoogleUserResult:
    """An uncommitted user decision to finalize with subsequent session persistence."""

    user: User = field(repr=False)
    created: bool


def _existing_google_user(identity: VerifiedGoogleIdentity, stored: AuthIdentity, session: Session) -> GoogleUserResult:
    """Lock the user first, then recheck the identity binding."""
    user = find_user_for_update(session, stored.user_id)
    current = find_google_identity(session, identity.issuer, identity.subject)
    if user is None or user.password_hash is not None or current is None or current.user_id != user.id:
        # Account linking is unsupported; reject local accounts and invalid identity bindings.
        raise GoogleAccountUnavailable(
            failure=FailureDetails(stage="user_lock", error_type="GoogleAccountStateChanged")
        )
    return GoogleUserResult(user=user, created=False)


def get_or_create_google_user(identity: VerifiedGoogleIdentity, session: Session) -> GoogleUserResult:
    """Resolve a verified identity using a dedicated session; the caller commits or rolls back.

    Insert the new user and identity within the same savepoint. Under READ COMMITTED,
    an identity uniqueness conflict rolls back only that savepoint, removing the temporary
    user before a fresh query reads the winner's committed identity. User locks remain held
    after returning so subsequent refresh persistence can share the same transaction.
    """
    stage: FailureStage = "google_identity_lookup"
    try:
        stored = find_google_identity(session, identity.issuer, identity.subject)
        if stored is not None:
            stage = "user_lock"
            return _existing_google_user(identity, stored, session)
        try:
            with session.begin_nested():
                stage = "user_insert"
                user = add_google_user(session, identity.email)
                stage = "google_identity_insert"
                add_google_identity(session, user.id, identity.issuer, identity.subject)
        except IntegrityError as exc:
            # Do not mistake violations of other constraints for a concurrent login.
            if not (
                isinstance(exc.orig, UniqueViolation)
                and exc.orig.diag.constraint_name == "auth_identities_issuer_subject_uq"
            ):
                raise
            stage = "google_identity_lookup"
            stored = find_google_identity(session, identity.issuer, identity.subject)
            if stored is None:
                raise GoogleAccountUnavailable(
                    failure=FailureDetails(stage=stage, error_type="GoogleIdentityConflictMissing")
                ) from None
            stage = "user_lock"
            return _existing_google_user(identity, stored, session)
        return GoogleUserResult(user=user, created=True)
    except SQLAlchemyError as exc:
        raise GoogleAccountUnavailable(failure=FailureDetails.from_exception(stage, exc)) from None
