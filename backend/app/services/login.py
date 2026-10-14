"""Check local credentials before login limits and session issuance are wired."""

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.user import User
from app.repositories.users import find_local_user_by_email, local_email_key
from app.schemas.auth import LoginRequest
from app.security.login_limits import LoginFailureLimiter
from app.security.passwords import PasswordHashError, verify_dummy_password, verify_password


class InvalidCredentials(Exception):
    """로컬 계정이 없거나 비밀번호가 틀렸을 때 사용하는 공통 인증 오류."""

    def __init__(self) -> None:
        super().__init__("로그인 정보를 확인해 주세요.")


class LoginUnavailable(Exception):
    """DB 조회나 해시 검증에 실패했을 때 사용하는 오류. 인증 정보와 내부 오류 내용은 숨긴다."""

    def __init__(self) -> None:
        super().__init__("일시적으로 로그인을 처리할 수 없습니다.")


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
    except (SQLAlchemyError, PasswordHashError):
        raise LoginUnavailable() from None


def authenticate_limited_local_user(
    request: LoginRequest, session: Session, limiter: LoginFailureLimiter, client_ip: str
) -> User:
    """실패 제한을 적용한 뒤 비밀번호를 검사한다. DB·해시 오류는 실패 횟수에 넣지 않는다."""
    try:
        email_key = local_email_key(session, request.email)
    except (SQLAlchemyError, ValueError):
        raise LoginUnavailable() from None
    with limiter.attempt(email_key, client_ip) as attempt:
        try:
            user = authenticate_local_user(request, session)
        except InvalidCredentials:
            attempt.failed()
            raise
        attempt.succeeded()
        return user
