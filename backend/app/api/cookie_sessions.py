"""Cookie-authenticated CSRF retrieval, refresh rotation, and logout endpoints."""

from collections.abc import Iterator
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.auth import current_refresh, registration_session, request_auth_settings, require_refresh_csrf
from app.api.cookies import AuthCookieError, REFRESH_COOKIE_NAME, clear_auth_cookies, set_auth_cookies
from app.api.errors import ApiError, unauthenticated
from app.observability import FailureDetails, request_trace_id
from app.schemas.auth import CsrfResponse, RefreshResponse
from app.schemas.errors import ErrorResponse
from app.security.settings import AuthSettings
from app.security.tokens import VerifiedToken
from app.services.csrf import read_csrf_token
from app.services.logout import logout_refresh_session
from app.services.refresh import (
    InvalidRefresh,
    RefreshLimitReached,
    RefreshRace,
    RefreshReuseDetected,
    RefreshUnavailable,
    cancel_refresh_rotation,
    rotate_refresh_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def csrf_session(verified: Annotated[VerifiedToken, Depends(current_refresh)]) -> Iterator[Session]:
    """잘못된 JWT를 거부한 뒤에만 DB 세션을 연다."""
    yield from registration_session()


def mutation_session(verified: Annotated[VerifiedToken, Depends(require_refresh_csrf)]) -> Iterator[Session]:
    """Origin·JWT·CSRF를 모두 검증한 뒤에만 DB 세션을 연다."""
    yield from registration_session()


def prevent_auth_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.get(
    "/csrf",
    response_model=CsrfResponse,
    responses={code: {"model": ErrorResponse} for code in (401, 503)},
)
def csrf(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(csrf_session)],
    settings: Annotated[AuthSettings, Depends(request_auth_settings)],
) -> CsrfResponse:
    try:
        value = read_csrf_token(request.cookies[REFRESH_COOKIE_NAME], session, settings)
    except InvalidRefresh:
        raise unauthenticated() from None
    except RefreshUnavailable as exc:
        raise ApiError("temporarily_unavailable", failure=exc.failure) from None
    logger.info("event=csrf_read trace_id=%s", request_trace_id(request))
    prevent_auth_caching(response)
    return CsrfResponse(csrf_token=value)


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 409, 503)},
)
def refresh(
    request: Request,
    verified: Annotated[VerifiedToken, Depends(require_refresh_csrf)],
    session: Annotated[Session, Depends(mutation_session)],
    settings: Annotated[AuthSettings, Depends(request_auth_settings)],
) -> JSONResponse:
    try:
        result = rotate_refresh_session(request.cookies[REFRESH_COOKIE_NAME], session, settings)
    except RefreshRace as exc:
        raise ApiError("refresh_race", retry_after=exc.retry_after) from None
    except (RefreshReuseDetected, RefreshLimitReached) as exc:
        # 이 예외들은 계보 폐기 커밋 이후에만 서비스에서 전달된다.
        reason = "reuse" if isinstance(exc, RefreshReuseDetected) else "limit"
        logger.info("event=refresh_lineage_revoked trace_id=%s reason=%s", request_trace_id(request), reason)
        raise ApiError("unauthenticated", clear_cookies=True) from None
    except InvalidRefresh as exc:
        raise ApiError("unauthenticated", clear_cookies=exc.clear_cookies) from None
    except RefreshUnavailable as exc:
        raise ApiError("temporarily_unavailable", failure=exc.failure) from None
    # 저장 결과와 HTTP 응답 결과는 구분한다. 이후 응답 준비 실패는 보상 로그로 연결한다.
    logger.info("event=refresh_rotated trace_id=%s", request_trace_id(request))
    try:
        # 부분 Set-Cookie와 직렬화 실패가 응답 밖으로 새지 않게 모두 준비한 뒤 반환한다.
        cookies = Response()
        csrf_value = set_auth_cookies(cookies, result.refresh, settings)
        body = RefreshResponse(access_token=result.access.token, csrf_token=csrf_value)
        response = JSONResponse(content=body.model_dump(mode="json"))
        response.raw_headers.extend((key, value) for key, value in cookies.raw_headers if key == b"set-cookie")
        prevent_auth_caching(response)
        return response
    except Exception as exc:
        try:
            cancel_refresh_rotation(verified.user_id, result.refresh, session, settings)
        except RefreshUnavailable as cancellation:
            logger.error(
                "event=refresh_compensation_failed trace_id=%s stage=%s error_type=%s",
                request_trace_id(request),
                cancellation.failure.stage,
                cancellation.failure.error_type,
            )
        else:
            logger.info("event=refresh_cancelled trace_id=%s", request_trace_id(request))
        if isinstance(exc, (AuthCookieError, ValueError, TypeError, OverflowError)):
            raise ApiError("temporarily_unavailable", failure=FailureDetails.from_exception("cookie", exc)) from None
        raise


@router.post(
    "/logout",
    status_code=204,
    response_class=Response,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 503)},
)
def logout(
    request: Request,
    session: Annotated[Session, Depends(mutation_session)],
    settings: Annotated[AuthSettings, Depends(request_auth_settings)],
) -> Response:
    try:
        logout_refresh_session(request.cookies[REFRESH_COOKIE_NAME], session, settings)
    except InvalidRefresh:
        raise unauthenticated() from None
    except RefreshUnavailable as exc:
        raise ApiError("temporarily_unavailable", failure=exc.failure) from None
    logger.info("event=logout_saved trace_id=%s", request_trace_id(request))
    response = Response(status_code=204)
    try:
        clear_auth_cookies(response, settings)
    except (AuthCookieError, ValueError, TypeError, OverflowError) as exc:
        raise ApiError("temporarily_unavailable", failure=FailureDetails.from_exception("cookie", exc)) from None
    prevent_auth_caching(response)
    return response
