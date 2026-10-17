"""Local credential checks, login session issuance, and session compensation."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.user import User
from app.observability import FailureDetails, FailureStage
from app.repositories.refresh_sessions import (
    add_initial_refresh_session,
    delete_refresh_session,
    find_refresh_session_for_update,
)
from app.repositories.users import find_local_user_by_email, find_user_for_update, local_email_key
from app.schemas.auth import LoginRequest, PublicUser
from app.security.login_limits import LoginAttempt, LoginFailureLimiter
from app.security.passwords import PasswordHashError, verify_dummy_password, verify_password
from app.security.session_values import create_refresh_digest
from app.security.settings import AuthSettings
from app.security.tokens import IssuedToken, TokenIssueError, issue_access_token, issue_refresh_token


class InvalidCredentials(Exception):
    """로컬 계정이 없거나 비밀번호가 틀렸을 때 사용하는 공통 인증 오류."""

    def __init__(self) -> None:
        super().__init__("로그인 정보를 확인해 주세요.")


class LoginUnavailable(Exception):
    """DB 조회나 해시 검증에 실패했을 때 사용하는 오류. 인증 정보와 내부 오류 내용은 숨긴다."""

    def __init__(self, *, failure: FailureDetails | None = None) -> None:
        super().__init__("일시적으로 로그인을 처리할 수 없습니다.")
        self.failure = failure


def authenticate_local_user(request: LoginRequest, session: Session) -> User:
    """비밀번호가 일치하는 로컬 사용자를 반환한다. 트랜잭션은 호출하는 쪽에서 관리한다.

    여기서는 커밋하거나 세션·토큰을 만들지 않는다.
    비밀번호는 입력한 그대로 검증하고, 사용자 ID는 클라이언트 입력이 아닌 DB 조회 결과를 사용한다.
    """
    try:
        user = find_local_user_by_email(session, request.email)
        if user is None or user.password_hash is None:
            verify_dummy_password(request.password)
            raise InvalidCredentials()
        if not verify_password(request.password, user.password_hash):
            raise InvalidCredentials()
        return user
    except SQLAlchemyError as exc:
        raise LoginUnavailable(failure=FailureDetails.from_exception("email_lookup", exc)) from None
    except PasswordHashError as exc:
        raise LoginUnavailable(failure=FailureDetails.from_exception("password_verify", exc)) from None


def authenticate_limited_local_user(
    request: LoginRequest, session: Session, limiter: LoginFailureLimiter, client_ip: str
) -> User:
    """실패 제한을 적용한 뒤 비밀번호를 검사한다. DB·해시 오류는 실패 횟수에 넣지 않는다."""
    with local_login_attempt(request, session, limiter, client_ip) as attempt:
        user = authenticate_local_user(request, session)
        attempt.succeeded()
        return user


def _rollback(session: Session, stage: FailureStage = "rollback") -> None:
    try:
        session.rollback()
    except SQLAlchemyError as exc:
        raise LoginUnavailable(failure=FailureDetails.from_exception(stage, exc)) from None


@contextmanager
def local_login_attempt(
    request: LoginRequest, session: Session, limiter: LoginFailureLimiter, client_ip: str
) -> Iterator[LoginAttempt]:
    """검사부터 응답 준비까지 자리를 예약하고, 자격 증명 검사 실패만 횟수에 반영한다."""
    try:
        email_key = local_email_key(session, request.email)
    except (SQLAlchemyError, ValueError) as exc:
        _rollback(session)
        raise LoginUnavailable(failure=FailureDetails.from_exception("email_lookup", exc)) from None
    # 제한에 걸린 요청이 DB 트랜잭션을 열어 둔 채 끝나지 않도록 한다.
    _rollback(session)
    with limiter.attempt(email_key, client_ip) as attempt:
        try:
            yield attempt
        except InvalidCredentials:
            attempt.failed()
            raise


@dataclass(frozen=True, slots=True)
class LoginResult:
    """저장이 끝난 로그인 세션의 공개 사용자 정보와 발급 토큰."""

    user: PublicUser
    access: IssuedToken
    refresh: IssuedToken


def create_login_session(request: LoginRequest, session: Session, settings: AuthSettings) -> LoginResult:
    """기존 사용자를 확인하고, 새 독립 refresh 세션만 저장한다."""
    stage: FailureStage = "password_verify"
    try:
        verified = authenticate_local_user(request, session)
        user_id, password_hash = verified.id, verified.password_hash
        # 조회 트랜잭션을 끝낸 뒤 짧은 저장 트랜잭션에서 사용자부터 잠근다.
        _rollback(session)
        stage = "user_lock"
        user = find_user_for_update(session, user_id)
        if user is None or user.password_hash != password_hash:
            raise InvalidCredentials()
        stage = "token_issue"
        now = datetime.now(timezone.utc)
        access = issue_access_token(user.id, settings, now=now)
        refresh = issue_refresh_token(user.id, settings, now=now)
        stage = "refresh_session"
        add_initial_refresh_session(
            session, user.id, create_refresh_digest(refresh.token, settings), refresh.issued_at, refresh.expires_at
        )
        result = LoginResult(user=PublicUser.model_validate(user), access=access, refresh=refresh)
        stage = "commit"
        session.commit()
        return result
    except (InvalidCredentials, LoginUnavailable):
        _rollback(session)
        raise
    except (SQLAlchemyError, TokenIssueError) as exc:
        _rollback(session)
        raise LoginUnavailable(failure=FailureDetails.from_exception(stage, exc)) from None
    except Exception:
        _rollback(session)
        raise


def cancel_login(result: LoginResult, session: Session, settings: AuthSettings) -> None:
    """쿠키 설정에 실패한 이번 로그인 세션만 취소한다. 기존 사용자와 다른 세션은 보존한다."""
    cancel_login_refresh_session(int(result.user.id), result.refresh, session, settings)


def cancel_login_refresh_session(user_id: int, issued: IssuedToken, session: Session, settings: AuthSettings) -> None:
    """Cancel one committed login root while preserving its user and other sessions."""
    try:
        user = find_user_for_update(session, user_id)
        if user is None:
            _rollback(session, "compensation_rollback")
            return
        refresh = find_refresh_session_for_update(session, user.id, create_refresh_digest(issued.token, settings))
        if refresh is None:
            _rollback(session, "compensation_rollback")
            return
        if (
            refresh.rotated_from_id is not None
            or refresh.revoked_at is not None
            or refresh.issued_at != issued.issued_at
            or refresh.expires_at != issued.expires_at
        ):
            raise LoginUnavailable(failure=FailureDetails(stage="compensation", error_type="LoginStateChanged"))
        delete_refresh_session(session, refresh)
        session.commit()
    except LoginUnavailable:
        _rollback(session, "compensation_rollback")
        raise
    except SQLAlchemyError as exc:
        _rollback(session, "compensation_rollback")
        raise LoginUnavailable(failure=FailureDetails.from_exception("compensation", exc)) from None
    except Exception:
        _rollback(session, "compensation_rollback")
        raise
