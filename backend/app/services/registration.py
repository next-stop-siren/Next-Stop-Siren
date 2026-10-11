"""Store a local user and initial refresh session as one transaction."""

from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.repositories.refresh_sessions import add_initial_refresh_session
from app.repositories.users import add_local_user, find_local_user_by_email
from app.schemas.auth import PublicUser, RegisterRequest
from app.security.passwords import PasswordHashError, hash_password
from app.security.session_values import create_refresh_digest
from app.security.settings import AuthSettings
from app.security.tokens import IssuedToken, TokenIssueError, issue_access_token, issue_refresh_token


class LocalEmailExists(Exception):
    """로컬 이메일 중복으로 가입 실패."""


class RegistrationUnavailable(Exception):
    """내부 정보를 노출하지 않는 저장·토큰 발급 오류."""


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """커밋 성공 후 반환할 사용자 정보와 토큰."""

    user: PublicUser
    access: IssuedToken
    refresh: IssuedToken


def _rollback(session: Session) -> None:
    try:
        session.rollback()
    except SQLAlchemyError:
        raise RegistrationUnavailable("가입 저장을 완료할 수 없습니다.") from None


def register_local_user(request: RegisterRequest, session: Session, settings: AuthSettings) -> RegistrationResult:
    """가입 전용 DB 세션을 사용하며, HTTP 응답·쿠키는 별도로 처리한다."""
    try:
        # 트랜잭션 시작 전에 비밀번호를 해싱한다.
        password_hash = hash_password(request.password)
        if find_local_user_by_email(session, request.email) is not None:
            raise LocalEmailExists("이미 가입된 로컬 이메일입니다.")
        user = add_local_user(session, request.email, password_hash)
        now = datetime.now(timezone.utc)
        access = issue_access_token(user.id, settings, now=now)
        refresh = issue_refresh_token(user.id, settings, now=now)
        add_initial_refresh_session(
            session,
            user.id,
            create_refresh_digest(refresh.token, settings),
            refresh.issued_at,
            refresh.expires_at,
        )
        # 커밋 후 DB 재조회 없이 반환하도록 미리 응답 정보를 만든다.
        result = RegistrationResult(user=PublicUser.model_validate(user), access=access, refresh=refresh)
        session.commit()
        return result
    except LocalEmailExists:
        _rollback(session)
        raise
    except IntegrityError as exc:
        _rollback(session)
        if isinstance(exc.orig, UniqueViolation) and exc.orig.diag.constraint_name == "users_local_email_uq":
            raise LocalEmailExists("이미 가입된 로컬 이메일입니다.") from None
        raise RegistrationUnavailable("가입 저장을 완료할 수 없습니다.") from None
    except (SQLAlchemyError, PasswordHashError, TokenIssueError):
        _rollback(session)
        raise RegistrationUnavailable("가입 저장을 완료할 수 없습니다.") from None
    except Exception:
        # 코드 오류도 롤백하되 원래 예외를 유지한다.
        _rollback(session)
        raise
