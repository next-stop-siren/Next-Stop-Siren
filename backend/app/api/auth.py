"""HTTP registration/login endpoints and authentication request dependencies."""

from collections.abc import Iterator
from ipaddress import ip_address
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.cookies import AuthCookieError, CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME, set_auth_cookies
from app.api.errors import ApiError, unauthenticated
from app.db import get_session
from app.observability import (
    FailureDetails,
    login_cancelled,
    login_compensation_failed,
    login_saved,
    registration_cancelled,
    registration_compensation_failed,
    registration_saved,
)
from app.schemas.auth import LoginRequest, LoginResponse, RegisterRequest, RegisterResponse
from app.schemas.errors import ErrorResponse
from app.security.settings import AuthSettings
from app.security.login_limits import LoginFailureLimiter
from app.security.session_values import verify_csrf_token
from app.security.tokens import TokenVerifyError, VerifiedToken, verify_access, verify_refresh_token
from app.services.login import (
    InvalidCredentials,
    LoginUnavailable,
    cancel_login,
    create_login_session,
    local_login_attempt,
)
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


def request_login_limiter(request: Request) -> LoginFailureLimiter:
    """요청마다 새 카운터를 만들지 않고 앱에 저장된 제한 모듈을 사용한다."""
    limiter = getattr(request.app.state, "login_failure_limiter", None)
    if not isinstance(limiter, LoginFailureLimiter):
        raise ApiError("temporarily_unavailable")
    return limiter


def request_client_ip(request: Request) -> str:
    """ASGI 연결 정보에서 IP를 읽는다. 임의의 전달 헤더는 직접 신뢰하지 않는다."""
    if request.client is None:
        raise ApiError("temporarily_unavailable")
    try:
        return str(ip_address(request.client.host))
    except ValueError:
        raise ApiError("temporarily_unavailable") from None


def require_allowed_origin(request: Request, settings: Annotated[AuthSettings, Depends(request_auth_settings)]) -> None:
    """Origin은 정규화 없이 허용 목록과 정확히 비교한다."""
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] not in settings.allowed_origins:
        raise ApiError("csrf_failed")


def current_refresh(
    request: Request, settings: Annotated[AuthSettings, Depends(request_auth_settings)]
) -> VerifiedToken:
    """refresh 쿠키의 JWT만 검증한다. DB의 존재·폐기 상태는 서비스에서 확인한다."""
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        raise unauthenticated()
    try:
        return verify_refresh_token(token, settings)
    except TokenVerifyError:
        raise unauthenticated() from None


def require_refresh_csrf(
    request: Request,
    origin: Annotated[None, Depends(require_allowed_origin)],
    refresh: Annotated[VerifiedToken, Depends(current_refresh)],
    settings: Annotated[AuthSettings, Depends(request_auth_settings)],
    csrf_header: Annotated[
        str | None,
        Header(
            alias="X-CSRF-Token", description="GET /api/auth/csrf 또는 최근 refresh 응답의 csrf_token을 입력하세요."
        ),
    ] = None,
) -> VerifiedToken:
    """refresh·logout의 변경 전 검증. 실패해도 DB나 쿠키를 변경하지 않는다."""
    # Header 선언은 Swagger 입력용이다. 중복 헤더 거부와 검증은 원본 목록을 사용한다.
    headers = request.headers.getlist("x-csrf-token")
    if len(headers) != 1 or not verify_csrf_token(
        refresh.jti, request.cookies.get(CSRF_COOKIE_NAME), headers[0], settings
    ):
        raise ApiError("csrf_failed")
    return refresh


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


def login_session() -> Iterator[Session]:
    """로그인에서도 기존의 안전한 DB 세션 생성 처리를 재사용한다."""
    yield from registration_session()


@router.post(
    "/login",
    status_code=200,
    response_model=LoginResponse,
    dependencies=[Depends(require_allowed_origin)],
    responses={code: {"model": ErrorResponse} for code in (401, 403, 422, 429, 503)},
)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    client_ip: Annotated[str, Depends(request_client_ip)],
    limiter: Annotated[LoginFailureLimiter, Depends(request_login_limiter)],
    session: Annotated[Session, Depends(login_session)],
    settings: Annotated[AuthSettings, Depends(request_auth_settings)],
) -> LoginResponse:
    """실패 제한과 비밀번호 검사를 거쳐, 저장된 새 로그인 세션을 반환한다."""
    try:
        with local_login_attempt(body, session, limiter, client_ip) as attempt:
            result = create_login_session(body, session, settings)
            login_saved(request)
            try:
                csrf_token = set_auth_cookies(response, result.refresh, settings)
            except AuthCookieError as exc:
                try:
                    cancel_login(result, session, settings)
                except LoginUnavailable as cancellation:
                    failure = cancellation.failure or FailureDetails.from_exception("compensation", cancellation)
                    login_compensation_failed(request, failure)
                else:
                    login_cancelled(request)
                raise ApiError(
                    "temporarily_unavailable", failure=FailureDetails.from_exception("cookie", exc)
                ) from None
            body_response = LoginResponse(user=result.user, access_token=result.access.token, csrf_token=csrf_token)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            # 저장과 쿠키 설정까지 성공한 요청만 이메일 실패 기록을 비운다.
            attempt.succeeded()
            return body_response
    except InvalidCredentials:
        raise unauthenticated() from None
    except LoginUnavailable as exc:
        failure = exc.failure or FailureDetails.from_exception("login", exc)
        raise ApiError("temporarily_unavailable", failure=failure) from None


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


access_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AccessBearer",
    bearerFormat="JWT",
    description="로그인 응답의 access_token만 입력하세요. Bearer 접두사는 자동으로 붙습니다.",
)
_COMPACT_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _verified_access(request: Request, credentials: HTTPAuthorizationCredentials | None) -> VerifiedToken:
    if (
        len(request.headers.getlist("authorization")) != 1
        or credentials is None
        or _COMPACT_JWT.fullmatch(credentials.credentials) is None
    ):
        raise unauthenticated()
    # 인증 정보가 없거나 잘못된 요청은 DB 세션을 열기 전에 거부한다.
    settings = request_auth_settings(request)
    try:
        return verify_access(credentials.credentials, settings)
    except TokenVerifyError:
        raise unauthenticated() from None


def current_user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(access_bearer)],
) -> int:
    """Bearer access를 검증한 사용자 ID만 전달한다. 쿠키와 입력 본문은 인증에 사용하지 않는다."""
    return _verified_access(request, credentials).user_id


def current_access(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(access_bearer)],
) -> VerifiedToken:
    """`current_user_id`와 같은 검증에 access 만료 시각을 더해 전달한다. 실시간 연결이 만료에 끊을 때 쓴다."""
    return _verified_access(request, credentials)


def authenticated_session() -> Iterator[Session]:
    """보호 경로에서도 안전한 DB 세션 생성 처리를 재사용한다."""
    yield from registration_session()
