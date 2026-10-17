"""Normal refresh rotation: revoke the predecessor and persist one successor atomically."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.observability import FailureDetails, FailureStage
from app.repositories.refresh_sessions import (
    MAX_REFRESH_LINEAGE_ISSUANCES,
    CorruptRefreshLineage,
    add_rotated_refresh_session,
    revoke_refresh_session,
)
from app.security.session_values import create_refresh_digest
from app.security.settings import AuthSettings
from app.security.tokens import (
    IssuedToken,
    TokenIssueError,
    TokenVerifyError,
    issue_access_token,
    issue_refresh_token,
    verify_refresh_token,
)
from app.services.refresh_sessions import lock_refresh_lineage


class InvalidRefresh(Exception):
    """없는 세션·만료·폐기 refresh는 토큰이나 내부 이유 없이 거부한다."""

    def __init__(self) -> None:
        super().__init__("유효한 refresh 세션이 필요합니다.")


class RefreshLimitReached(InvalidRefresh):
    """4096번째 발급은 차단한다. 한도 도달 시 계보 폐기는 후속 단계에서 연결한다."""


class RefreshUnavailable(Exception):
    """저장·발급·계보 오류는 안전한 실패 단계만 전달한다."""

    def __init__(self, *, failure: FailureDetails) -> None:
        super().__init__("일시적으로 refresh를 처리할 수 없습니다.")
        self.failure = failure


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """커밋 후에만 반환하는 access와 refresh. HTTP 쿠키 설정은 호출자가 맡는다."""

    access: IssuedToken = field(repr=False)
    refresh: IssuedToken = field(repr=False)


def _rollback(session: Session) -> None:
    try:
        session.rollback()
    except SQLAlchemyError as exc:
        raise RefreshUnavailable(failure=FailureDetails.from_exception("rollback", exc)) from None


def rotate_refresh_session(token: str, session: Session, settings: AuthSettings) -> RefreshResult:
    """Origin·CSRF 검사 후 전용 DB 세션으로 호출한다. HTTP 응답은 만들지 않는다.

    JWT를 검증하고 사용자 잠금을 얻은 뒤 만료를 다시 확인한다. 이전 세션 폐기와
    후임 digest 저장은 함께 커밋한다. 경합·재사용·한도 폐기의 상세 처리는 Phase 5에서 확장한다.
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
        # 잠금을 기다리는 사이 만료될 수 있으므로, 잠금 이후의 시각으로 다시 판정한다.
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
        if len(lineage) >= MAX_REFRESH_LINEAGE_ISSUANCES:
            raise RefreshLimitReached()
        stage = "token_issue"
        access = issue_access_token(verified.user_id, settings, now=now)
        refresh = issue_refresh_token(verified.user_id, settings, now=now, absolute_expires_at=lineage[0].expires_at)
        stage = "refresh_session"
        revoke_refresh_session(current, now)
        add_rotated_refresh_session(session, current, create_refresh_digest(refresh.token, settings), refresh.issued_at)
        result = RefreshResult(access=access, refresh=refresh)
        stage = "commit"
        session.commit()
        return result
    except InvalidRefresh:
        _rollback(session)
        raise
    except TokenVerifyError:
        _rollback(session)
        raise InvalidRefresh() from None
    except CorruptRefreshLineage as exc:
        _rollback(session)
        raise RefreshUnavailable(failure=FailureDetails.from_exception("refresh_session", exc)) from None
    except (SQLAlchemyError, TokenIssueError) as exc:
        _rollback(session)
        raise RefreshUnavailable(failure=FailureDetails.from_exception(stage, exc)) from None
    except Exception:
        _rollback(session)
        raise
