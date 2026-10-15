"""Convert expected API failures into safe, stable public responses."""

import logging
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.observability import FailureDetails, request_trace_id
from app.schemas.errors import ErrorDetail, ErrorResponse
from app.security.login_limits import LoginRateLimited

logger = logging.getLogger(__name__)

# 연결·조회 시간 제한·풀 대기는 503, 다른 DB 오류는 원래 예외를 유지한다.
DATABASE_UNAVAILABLE = (OperationalError, InterfaceError, PoolTimeoutError)
ApiErrorCode = Literal[
    "local_email_exists",
    "csrf_failed",
    "temporarily_unavailable",
    "unauthenticated",
    "not_found",
    "rate_limited",
    "request_key_conflict",
    "conversation_busy",
    "provider_failed",
    "provider_timeout",
]

_API_ERRORS: dict[ApiErrorCode, tuple[int, str]] = {
    "local_email_exists": (409, "이미 가입된 이메일입니다."),
    "csrf_failed": (403, "요청 출처 또는 CSRF 값을 확인해 주세요."),
    "temporarily_unavailable": (503, "일시적으로 서비스를 이용할 수 없습니다."),
    "unauthenticated": (401, "로그인이 필요합니다."),
    "not_found": (404, "찾을 수 없습니다."),
    "rate_limited": (429, "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요."),
    "request_key_conflict": (409, "같은 요청 키로 다른 질문을 보낼 수 없습니다."),
    "conversation_busy": (409, "이전 질문의 답변을 기다리고 있습니다."),
    "provider_failed": (502, "답변을 만들지 못했습니다. 대화 기록을 확인해 주세요."),
    "provider_timeout": (504, "답변 시간이 초과되었습니다. 대화 기록을 확인해 주세요."),
}
_VALIDATION_MESSAGES = {
    "missing": "필수 값입니다.",
    "string_type": "문자열로 입력해 주세요.",
    "extra_forbidden": "허용하지 않은 필드가 포함되어 있습니다.",
    "json_invalid": "올바른 JSON 본문을 보내 주세요.",
}
_LOCATIONS = {"body", "path", "query", "header", "cookie"}
_PUBLIC_FIELDS = {
    "email",
    "password",
    "limit",
    "before_id",
    "after_id",
    "conversation_id",
    "content",
    "request_key",
    "return_to",
}


class ApiError(Exception):
    """예상 가능한 오류는 내부 메시지 대신 정해진 코드를 사용한다."""

    def __init__(
        self, code: ApiErrorCode, *, failure: FailureDetails | None = None, retry_after: int | None = None
    ) -> None:
        if code == "rate_limited":
            if type(retry_after) is not int or retry_after < 1:
                raise ValueError("로그인 제한에는 양의 정수 대기 시간이 필요합니다.")
        elif retry_after is not None:
            raise ValueError("대기 시간은 로그인 제한 오류에만 지정할 수 있습니다.")
        self.code = code
        self.status_code, self.public_message = _API_ERRORS[code]
        self.failure = failure
        self.retry_after = retry_after
        super().__init__(code)


def unauthenticated() -> ApiError:
    return ApiError("unauthenticated")


def not_found() -> ApiError:
    """없는 리소스와 다른 사용자의 리소스에 같은 오류를 반환한다."""
    return ApiError("not_found")


def request_key_conflict() -> ApiError:
    return ApiError("request_key_conflict")


def conversation_busy() -> ApiError:
    return ApiError("conversation_busy")


def provider_failed() -> ApiError:
    return ApiError("provider_failed")


def provider_timeout() -> ApiError:
    return ApiError("provider_timeout")


def loggable(exc: Exception) -> str:
    """SQL·입력값 없이 예외 클래스와 SQLSTATE만 기록한다."""
    names = [type(exc).__name__]
    cause = getattr(exc, "orig", None)
    if cause is not None:
        names.append(type(cause).__name__)
        sqlstate = getattr(cause, "sqlstate", None)
        if isinstance(sqlstate, str):
            names.append(sqlstate)
    return "/".join(names)


def _error_response(
    status_code: int, code: str, message: str, fields: dict[str, str], trace_id: str | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, fields=fields, trace_id=trace_id or str(uuid4()))
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):
        raise exc
    trace_id = request_trace_id(request)
    if exc.failure is not None:
        operation = (
            "login"
            if request.url.path == "/api/auth/login"
            else "registration"
            if request.url.path == "/api/auth/register"
            else "authentication"
        )
        logger.error(
            "event=%s_failed trace_id=%s stage=%s error_type=%s",
            operation,
            trace_id,
            exc.failure.stage,
            exc.failure.error_type,
        )
    response = _error_response(exc.status_code, exc.code, exc.public_message, {}, trace_id=trace_id)
    if exc.code == "unauthenticated":
        response.headers["WWW-Authenticate"] = "Bearer"
    if exc.retry_after is not None:
        response.headers["Retry-After"] = str(exc.retry_after)
    return response


async def login_rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    """제한 모듈의 오류를 공통 429 응답으로 바꾼다."""
    if not isinstance(exc, LoginRateLimited):
        raise exc
    return await api_error_handler(request, ApiError("rate_limited", retry_after=exc.retry_after))


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    fields: dict[str, str] = {}
    for error in exc.errors():
        location = error.get("loc", ())
        root = location[0] if location and location[0] in _LOCATIONS else "body"
        # 임의 필드명·중첩 키는 숨기고, 공개 필드명만 표시한다.
        field = root
        if error.get("type") != "extra_forbidden" and len(location) > 1 and location[1] in _PUBLIC_FIELDS:
            field = location[1]
        message = _VALIDATION_MESSAGES.get(error.get("type", ""), "입력값을 확인해 주세요.")
        fields.setdefault(field, message)
    return _error_response(422, "validation_error", "요청을 확인해 주세요.", fields, trace_id=request_trace_id(request))


async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    # 응답과 로그를 같은 trace ID로 연결하며, 예외 원문은 기록하지 않는다.
    trace_id = request_trace_id(request)
    logger.error("Database unavailable, trace_id=%s, error=%s", trace_id, loggable(exc))
    return _error_response(503, "temporarily_unavailable", "잠시 후 다시 시도해 주세요.", {}, trace_id=trace_id)


def register_error_handlers(app: FastAPI) -> None:
    """입력·예상 오류와 DB 연결 실패 핸들러를 등록한다."""
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(LoginRateLimited, login_rate_limit_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    for kind in DATABASE_UNAVAILABLE:
        app.add_exception_handler(kind, database_unavailable_handler)
