"""HTTP registration endpoint and authentication request dependencies."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError, set_auth_cookies
from app.api.errors import ApiError, unauthenticated
from app.db import get_session
from app.observability import (
    FailureDetails,
    registration_cancelled,
    registration_compensation_failed,
    registration_saved,
)
from app.schemas.auth import RegisterRequest, RegisterResponse
from app.schemas.errors import ErrorResponse
from app.security.settings import AuthSettings
from app.services.registration import (
    LocalEmailExists,
    RegistrationUnavailable,
    cancel_registration,
    register_local_user,
)


def request_auth_settings(request: Request) -> AuthSettings:
    """CORS와 토큰 발급에 같은 설정을 사용한다."""
    settings = request.app.state.auth_settings
    if not isinstance(settings, AuthSettings):
        raise ApiError("temporarily_unavailable")
    return settings


def require_allowed_origin(request: Request, settings: Annotated[AuthSettings, Depends(request_auth_settings)]) -> None:
    """Origin은 정규화 없이 허용 목록과 정확히 비교한다."""
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] not in settings.allowed_origins:
        raise ApiError("csrf_failed")


def registration_session() -> Iterator[Session]:
    """DB 세션 생성 실패를 공개 오류로 바꾼다."""
    sessions = get_session()
    try:
        try:
            session = next(sessions)
        except (SQLAlchemyError, RuntimeError, ValueError) as exc:
            raise ApiError("temporarily_unavailable", failure=FailureDetails.from_exception("session", exc)) from None
        yield session
    finally:
        sessions.close()


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=201,
    response_model=RegisterResponse,
    dependencies=[Depends(require_allowed_origin)],
    responses={code: {"model": ErrorResponse} for code in (403, 409, 422, 503)},
)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(registration_session)],
    settings: Annotated[AuthSettings, Depends(request_auth_settings)],
) -> RegisterResponse:
    """저장 커밋이 성공한 뒤에만 토큰과 쿠키를 반환한다."""
    try:
        result = register_local_user(body, session, settings)
    except LocalEmailExists:
        raise ApiError("local_email_exists") from None
    except RegistrationUnavailable as exc:
        failure = exc.failure or FailureDetails.from_exception("registration", exc)
        raise ApiError("temporarily_unavailable", failure=failure) from None
    registration_saved(request)
    try:
        csrf_token = set_auth_cookies(response, result.refresh, settings)
    except AuthCookieError as exc:
        try:
            cancel_registration(result, session, settings)
        except RegistrationUnavailable as cancellation:
            failure = cancellation.failure or FailureDetails.from_exception("compensation", cancellation)
            registration_compensation_failed(request, failure)
        else:
            registration_cancelled(request)
        raise ApiError("temporarily_unavailable", failure=FailureDetails.from_exception("cookie", exc)) from None
    # 인증 정보가 브라우저나 프록시 캐시에 남지 않게 한다.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return RegisterResponse(user=result.user, access_token=result.access.token, csrf_token=csrf_token)


def current_user_id() -> int:
    """access 검증을 연결하기 전까지 보호된 요청을 거부한다."""
    raise unauthenticated()
