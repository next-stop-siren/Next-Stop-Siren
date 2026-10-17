"""Commit revocation of the current login lineage while preserving independent logins."""

from datetime import datetime, timezone
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.observability import FailureDetails, FailureStage
from app.repositories.refresh_sessions import CorruptRefreshLineage, revoke_refresh_lineage
from app.security.session_values import create_refresh_digest
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


def logout_refresh_session(token: str, session: Session, settings: AuthSettings) -> None:
    """Origin·CSRF 검사를 통과한 요청에서 전용 세션으로 호출한다.

    사용자 → 현재 refresh 순서로 잠근 뒤 현재 활성 세션의 계보만 폐기한다.
    커밋이 성공한 경우에만 정상 반환한다. API의 204 응답과 쿠키 만료는 호출자가 맡으며,
    이미 발급한 access JWT와 다른 로그인 계보는 변경하지 않는다.
    """
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
            or any(item.revoked_at is None for item in lineage[:-1])
        ):
            raise CorruptRefreshLineage()
        revoke_refresh_lineage(lineage, now)
        stage = "commit"
        session.commit()
    except InvalidRefresh:
        _rollback(session)
        raise
    except TokenVerifyError:
        _rollback(session)
        raise InvalidRefresh() from None
    except CorruptRefreshLineage as exc:
        logger.error("event=logout_lineage_corrupt error_type=%s", type(exc).__name__)
        _rollback(session)
        raise RefreshUnavailable(failure=FailureDetails.from_exception("refresh_session", exc)) from None
    except SQLAlchemyError as exc:
        _rollback(session)
        raise RefreshUnavailable(failure=FailureDetails.from_exception(stage, exc)) from None
    except Exception:
        _rollback(session)
        raise
