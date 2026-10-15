"""HTTP entry point for server-started Google OIDC login."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
import httpx

from app.api.auth import request_auth_settings
from app.api.errors import ApiError
from app.api.google_cookies import set_google_transaction_cookie
from app.integrations.google_oidc import (
    GOOGLE_DISCOVERY_TIMEOUT_SECONDS,
    GoogleProviderUnavailable,
    google_authorization_url,
)
from app.observability import FailureDetails
from app.schemas.errors import ErrorResponse
from app.security.google_transactions import (
    GoogleTransactionError,
    InvalidGoogleReturnTo,
    create_google_transaction,
    validate_google_return_to,
)
from app.security.settings import GoogleSettings

router = APIRouter(prefix="/api/auth/google", tags=["auth"])


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
    """공개 메타데이터 조회용 클라이언트를 요청마다 열고 반드시 닫는다."""
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
    responses={code: {"model": ErrorResponse} for code in (422, 503)},
)
async def google_start(
    request: Request,
    client: Annotated[httpx.AsyncClient, Depends(google_http_client)],
    return_to: Annotated[str | None, Query(description="로그인 후 이동할 허용된 상대 경로")] = None,
) -> RedirectResponse:
    """복귀 경로와 설정을 확인한 뒤 암호화 쿠키를 설정하고 Google로 이동한다."""
    if len(request.query_params.getlist("return_to")) > 1:
        raise invalid_return_to()
    try:
        destination = validate_google_return_to(return_to)
    except InvalidGoogleReturnTo:
        raise invalid_return_to() from None
    auth_settings = request_auth_settings(request)
    google_settings = request_google_settings(request)
    try:
        transaction = create_google_transaction(destination)
        url = await google_authorization_url(transaction, google_settings, client)
        response = RedirectResponse(url=url, status_code=302)
        set_google_transaction_cookie(response, transaction, google_settings, auth_settings)
    except GoogleProviderUnavailable as exc:
        raise ApiError("temporarily_unavailable", failure=exc.failure) from None
    except GoogleTransactionError as exc:
        raise ApiError(
            "temporarily_unavailable", failure=FailureDetails.from_exception("google_transaction", exc)
        ) from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
