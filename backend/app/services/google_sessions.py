"""Commit a verified Google account and its initial application refresh session."""

from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.observability import FailureDetails, FailureStage
from app.repositories.refresh_sessions import add_initial_refresh_session
from app.schemas.auth import PublicUser
from app.security.google_identity import VerifiedGoogleIdentity
from app.security.session_values import create_refresh_digest
from app.security.settings import AuthSettings
from app.security.tokens import IssuedToken, TokenIssueError, issue_refresh_token
from app.services.google_accounts import GoogleAccountUnavailable, get_or_create_google_user
from app.services.login import LoginUnavailable, cancel_login_refresh_session


@dataclass(frozen=True, slots=True)
class GoogleLoginResult:
    """Committed user and refresh metadata for subsequent cookie creation."""

    user: PublicUser = field(repr=False)
    refresh: IssuedToken = field(repr=False)


def _rollback(session: Session) -> None:
    try:
        session.rollback()
    except SQLAlchemyError as exc:
        raise LoginUnavailable(failure=FailureDetails.from_exception("rollback", exc)) from None


def create_google_login_session(
    identity: VerifiedGoogleIdentity, session: Session, settings: AuthSettings
) -> GoogleLoginResult:
    """Use a dedicated session after OIDC verification; commit all account and session writes together.

    Account resolution retains the user lock until commit. Only a refresh digest is persisted;
    access issuance belongs to the later refresh flow. No HTTP response or cookies are created here.
    """
    stage: FailureStage = "google_identity_lookup"
    try:
        account = get_or_create_google_user(identity, session)
        stage = "token_issue"
        refresh = issue_refresh_token(account.user.id, settings)
        stage = "refresh_session"
        add_initial_refresh_session(
            session,
            account.user.id,
            create_refresh_digest(refresh.token, settings),
            refresh.issued_at,
            refresh.expires_at,
        )
        # Prepare the public snapshot before commit to avoid a later ORM reload.
        result = GoogleLoginResult(user=PublicUser.model_validate(account.user), refresh=refresh)
        stage = "commit"
        session.commit()
        return result
    except GoogleAccountUnavailable as exc:
        _rollback(session)
        raise LoginUnavailable(failure=exc.failure) from None
    except (SQLAlchemyError, TokenIssueError) as exc:
        _rollback(session)
        raise LoginUnavailable(failure=FailureDetails.from_exception(stage, exc)) from None
    except Exception:
        _rollback(session)
        raise


def cancel_google_login(result: GoogleLoginResult, session: Session, settings: AuthSettings) -> None:
    """Cancel this refresh session if response preparation fails after commit.

    Preserve the account and identity: another login may already have adopted them.
    A repeated cancellation is harmless, but a rotated or otherwise changed root is rejected.
    """
    cancel_login_refresh_session(int(result.user.id), result.refresh, session, settings)
