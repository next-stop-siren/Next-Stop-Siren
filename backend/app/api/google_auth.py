"""HTTP start and callback endpoints for server-started Google OIDC login."""

from collections.abc import AsyncIterator
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
import httpx
from starlette.concurrency import run_in_threadpool

from app.api.auth import login_session, request_auth_settings
from app.api.cookies import set_auth_cookies
from app.api.errors import ApiError, api_error_handler
from app.api.google_cookies import (
    GOOGLE_TRANSACTION_COOKIE_NAME,
    clear_google_transaction_cookie,
    set_google_transaction_cookie,
)
from app.integrations.google_oidc import (
    GOOGLE_DISCOVERY_TIMEOUT_SECONDS,
    GoogleProviderUnavailable,
    google_authorization_url,
)
from app.observability import (
    FailureDetails,
    google_callback_verification_failed,
    google_login_failure,
    login_cancelled,
    login_compensation_failed,
    login_saved,
)
from app.schemas.errors import ErrorResponse
from app.security.google_transactions import (
    GoogleTransactionError,
    InvalidGoogleReturnTo,
    create_google_transaction,
    validate_google_return_to,
    verify_google_transaction,
)
from app.security.google_identity import GoogleIdentityError
from app.security.settings import AuthSettings, GoogleSettings
from app.services.google_login import VerifiedGoogleLogin, verify_google_login
from app.services.google_sessions import cancel_google_login, create_google_login_session
from app.services.login import LoginUnavailable

router = APIRouter(prefix="/api/auth/google", tags=["auth"])

GoogleLoginFailure = Literal["cancelled", "invalid_request", "temporarily_unavailable"]


def request_google_settings(request: Request) -> GoogleSettings:
    """앱 생성 때 검증한 Google 설정을 사용하고 누락 시 Google 경로만 차단한다."""
    settings = request.app.state.google_settings
    if not isinstance(settings, GoogleSettings):
        raise ApiError(
            "temporarily_unavailable",
            failure=FailureDetails(stage="google_settings", error_type="GoogleSettingsUnavailable"),
        )
    return settings


async def google_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Open a per-request Google client with bounded timeouts and no environment proxies."""
    async with httpx.AsyncClient(
        timeout=GOOGLE_DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
    ) as client:
        yield client


def invalid_return_to() -> RequestValidationError:
    """입력 원문 없이 기존 공통 422 응답 형식으로 복귀 경로 오류를 전달한다."""
    return RequestValidationError(
        [{"type": "value_error", "loc": ("query", "return_to"), "msg": "허용된 복귀 경로가 필요합니다."}]
    )


@router.get(
    "/start",
    status_code=302,
    response_class=RedirectResponse,
    responses={
        302: {"description": "Google 인증 시작 또는 안전한 화면으로 실패 복귀"},
        **{code: {"model": ErrorResponse} for code in (422, 503)},
    },
)
async def google_start(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(google_http_client)],
    return_to: Annotated[str | None, Query(description="로그인 후 이동할 허용된 상대 경로")] = None,
) -> Response:
    """복귀 경로와 설정을 확인한 뒤 암호화 쿠키를 설정하고 Google로 이동한다."""
    if len(request.query_params.getlist("return_to")) > 1:
        raise invalid_return_to()
    try:
        destination = validate_google_return_to(return_to)
    except InvalidGoogleReturnTo:
        raise invalid_return_to() from None
    auth_settings: AuthSettings | None = None
    google_settings: GoogleSettings | None = None
    try:
        auth_settings = request_auth_settings(request)
        google_settings = request_google_settings(request)
        transaction = create_google_transaction(destination)
        url = await google_authorization_url(transaction, google_settings, client)
        try:
            response = RedirectResponse(url=url, status_code=302)
            set_google_transaction_cookie(response, transaction, google_settings, auth_settings)
            _callback_headers(response)
        except Exception as exc:
            # Discard any partial cookie headers; response preparation is a bounded failure boundary.
            raise ApiError("temporarily_unavailable", failure=FailureDetails.from_exception("cookie", exc)) from None
        return response
    except GoogleProviderUnavailable as exc:
        failure = ApiError("temporarily_unavailable", failure=exc.failure)
    except GoogleTransactionError as exc:
        failure = ApiError("temporarily_unavailable", failure=FailureDetails.from_exception("google_transaction", exc))
    except ApiError as exc:
        failure = exc
    return await _google_failure_response(request, failure, "temporarily_unavailable", auth_settings, google_settings)


def _callback_headers(response: Response) -> None:
    """Keep callback credentials out of caches and subsequent Referer headers."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


def _delete_transaction_cookie(response: Response, request: Request, settings: AuthSettings | None) -> None:
    """Delete the same host/path cookie even when settings or the normal cleanup helper are unavailable."""
    response.delete_cookie(
        GOOGLE_TRANSACTION_COOKIE_NAME,
        path=settings.cookie_path if settings is not None else "/api/auth",
        secure=settings.cookie_secure if settings is not None else request.url.scheme == "https",
        httponly=settings.cookie_httponly if settings is not None else True,
        samesite=settings.cookie_samesite if settings is not None else "lax",
    )


async def _google_failure_response(
    request: Request,
    failure: ApiError,
    reason: GoogleLoginFailure,
    settings: AuthSettings | None,
    google_settings: GoogleSettings | None,
) -> Response:
    """Log safe failures and build a fresh response using only validated configuration and fixed values."""
    # Preserve existing stage/error_type diagnostics and server trace IDs, including on redirects.
    response: Response = await api_error_handler(request, failure)
    google_login_failure(request, reason)
    try:
        if settings is not None and google_settings is not None:
            destination = (google_settings.frontend_origin or "") + "/?google_login_error=" + reason
            response = RedirectResponse(url=destination, status_code=302)
        if settings is not None:
            clear_google_transaction_cookie(response, settings)
        else:
            _delete_transaction_cookie(response, request, settings)
        _callback_headers(response)
    except Exception as exc:
        # Do not retry a broken redirect or reuse a response containing partial cookies.
        response = await api_error_handler(
            request, ApiError("temporarily_unavailable", failure=FailureDetails.from_exception("cookie", exc))
        )
        _delete_transaction_cookie(response, request, settings)
        _callback_headers(response)
    return response


def _finish_google_login(
    verified: VerifiedGoogleLogin, request: Request, settings: AuthSettings, google_settings: GoogleSettings
) -> RedirectResponse:
    """Open and close the synchronous DB session in one worker, after OIDC verification."""
    with contextmanager(login_session)() as session:
        try:
            result = create_google_login_session(verified.identity, session, settings)
        except LoginUnavailable as exc:
            raise ApiError(
                "temporarily_unavailable", failure=exc.failure or FailureDetails.from_exception("login", exc)
            ) from None
        login_saved(request)
        try:
            path = validate_google_return_to(verified.return_to)
            # Only validated server configuration and an allowlisted relative path decide the destination.
            destination = (google_settings.frontend_origin or "") + path
            response = RedirectResponse(url=destination, status_code=302)
            set_auth_cookies(response, result.refresh, settings)
            clear_google_transaction_cookie(response, settings)
            _callback_headers(response)
            return response
        except Exception as exc:
            # Discard the partial response, including any cookies already added to it.
            try:
                cancel_google_login(result, session, settings)
            except LoginUnavailable as cancellation:
                login_compensation_failed(
                    request, cancellation.failure or FailureDetails.from_exception("compensation", cancellation)
                )
            else:
                login_cancelled(request)
            raise ApiError("temporarily_unavailable", failure=FailureDetails.from_exception("cookie", exc)) from None


@router.get(
    "/callback",
    status_code=302,
    response_class=RedirectResponse,
    responses={
        302: {"description": "로그인 성공 또는 google_login_error로 분류한 안전한 실패 복귀"},
        503: {"model": ErrorResponse, "description": "설정 또는 실패 응답 준비 불가 시 안전한 JSON 대체 응답"},
    },
)
async def google_callback(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(google_http_client)],
    code: Annotated[str | None, Query(description="Google이 발급한 일회용 code")] = None,
    state: Annotated[str | None, Query(description="로그인 시작 요청과 연결하는 state")] = None,
    error: Annotated[str | None, Query(description="Google 로그인 취소 또는 오류")] = None,
    iss: Annotated[str | None, Query(description="Google이 선택적으로 전달하는 issuer")] = None,
) -> Response:
    """Verify before opening a DB session; issue cookies only after commit and clear the transaction."""
    settings: AuthSettings | None = None
    google_settings: GoogleSettings | None = None
    try:
        settings = request_auth_settings(request)
        google_settings = request_google_settings(request)
        if any(len(request.query_params.getlist(name)) > 1 for name in ("code", "state", "error", "iss")):
            raise GoogleIdentityError()
        if iss is not None and iss not in {google_settings.issuer, "accounts.google.com"}:
            raise GoogleIdentityError()
        if error is not None and code is not None:
            raise GoogleIdentityError()
        if error is not None:
            # Validate the transaction even on cancellation; never echo provider error text.
            verify_google_transaction(request.cookies.get(GOOGLE_TRANSACTION_COOKIE_NAME), state, google_settings)
            if error == "access_denied":
                return await _google_failure_response(
                    request, ApiError("unauthenticated"), "cancelled", settings, google_settings
                )
            if error in {"server_error", "temporarily_unavailable"}:
                return await _google_failure_response(
                    request, ApiError("temporarily_unavailable"), "temporarily_unavailable", settings, google_settings
                )
            raise GoogleIdentityError()
        verified = await verify_google_login(
            code, state, request.cookies.get(GOOGLE_TRANSACTION_COOKIE_NAME), google_settings, client
        )
        return await run_in_threadpool(_finish_google_login, verified, request, settings, google_settings)
    except (GoogleIdentityError, GoogleTransactionError):
        google_callback_verification_failed(request)
        failure = ApiError("unauthenticated")
    except GoogleProviderUnavailable as exc:
        failure = ApiError("temporarily_unavailable", failure=exc.failure)
    except ApiError as exc:
        failure = exc
    reason: GoogleLoginFailure = "invalid_request" if failure.code == "unauthenticated" else "temporarily_unavailable"
    return await _google_failure_response(request, failure, reason, settings, google_settings)
