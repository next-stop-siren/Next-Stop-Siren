"""Read CSRF only for a verified, active persisted refresh session."""

from datetime import datetime, timezone
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.observability import FailureDetails, FailureStage
from app.repositories.refresh_sessions import CorruptRefreshLineage
from app.security.session_values import create_csrf_token, create_refresh_digest
from app.security.settings import AuthSettings
from app.security.tokens import TokenVerifyError, verify_refresh_token
from app.services.refresh import InvalidRefresh, RefreshUnavailable
from app.services.refresh_sessions import lock_refresh_lineage

logger = logging.getLogger(__name__)


def _rollback(session: Session) -> None:
    try:
        session.rollback()
    except SQLAlchemyError as exc:
        raise RefreshUnavailable(failure=FailureDetails.from_exception("rollback", exc)) from None


def read_csrf_token(token: str, session: Session, settings: AuthSettings) -> str:
    """활성 세션에 대응하는 MAC만 읽고 잠금을 해제한다. 쿠키·DB 상태는 변경하지 않는다."""
    stage: FailureStage = "refresh_session"
    try:
        verified = verify_refresh_token(token, settings)
        stage = "user_lock"
        locked = lock_refresh_lineage(session, verified.user_id, create_refresh_digest(token, settings))
        if locked is None:
            raise InvalidRefresh()
        stage = "refresh_session"
        current, lineage = locked.current, locked.sessions
        now = datetime.now(timezone.utc)
        if current.revoked_at is not None or current.expires_at <= now or verified.expires_at <= now:
            raise InvalidRefresh()
        if (
            current.issued_at != verified.issued_at
            or current.expires_at != verified.expires_at
            or current.id != lineage[-1].id
            or any(row.revoked_at is None for row in lineage[:-1])
        ):
            raise CorruptRefreshLineage()
        csrf = create_csrf_token(verified.jti, settings)
    except InvalidRefresh:
        _rollback(session)
        raise
    except TokenVerifyError:
        _rollback(session)
        raise InvalidRefresh() from None
    except CorruptRefreshLineage as exc:
        logger.error("event=csrf_lineage_corrupt error_type=%s", type(exc).__name__)
        _rollback(session)
        raise RefreshUnavailable(failure=FailureDetails.from_exception("refresh_session", exc)) from None
    except SQLAlchemyError as exc:
        _rollback(session)
        raise RefreshUnavailable(failure=FailureDetails.from_exception(stage, exc)) from None
    except Exception:
        _rollback(session)
        raise
    _rollback(session)
    return csrf
