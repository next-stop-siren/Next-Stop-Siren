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

logger = logging.getLogger(__name__)

# 연결·조회 시간 제한·풀 대기는 503, 다른 DB 오류는 원래 예외를 유지한다.
DATABASE_UNAVAILABLE = (OperationalError, InterfaceError, PoolTimeoutError)
ApiErrorCode = Literal["local_email_exists", "csrf_failed", "temporarily_unavailable", "unauthenticated", "not_found"]

_API_ERRORS: dict[ApiErrorCode, tuple[int, str]] = {
    "local_email_exists": (409, "이미 가입된 이메일입니다."),
    "csrf_failed": (403, "요청 출처 또는 CSRF 값을 확인해 주세요."),
    "temporarily_unavailable": (503, "일시적으로 서비스를 이용할 수 없습니다."),
    "unauthenticated": (401, "로그인이 필요합니다."),
    "not_found": (404, "찾을 수 없습니다."),
}
_VALIDATION_MESSAGES = {
    "missing": "필수 값입니다.",
    "string_type": "문자열로 입력해 주세요.",
    "extra_forbidden": "허용하지 않은 필드가 포함되어 있습니다.",
    "json_invalid": "올바른 JSON 본문을 보내 주세요.",
}
_LOCATIONS = {"body", "path", "query", "header", "cookie"}
_PUBLIC_FIELDS = {"email", "password", "limit", "before_id", "after_id", "conversation_id"}


class ApiError(Exception):
    """예상 가능한 오류는 내부 메시지 대신 정해진 코드를 사용한다."""

    def __init__(self, code: ApiErrorCode, *, failure: FailureDetails | None = None) -> None:
        self.code = code
        self.status_code, self.public_message = _API_ERRORS[code]
        self.failure = failure
        super().__init__(code)


def unauthenticated() -> ApiError:
    return ApiError("unauthenticated")


def not_found() -> ApiError:
    """없는 리소스와 다른 사용자의 리소스에 같은 오류를 반환한다."""
    return ApiError("not_found")


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
        logger.error(
            "event=registration_failed trace_id=%s stage=%s error_type=%s",
            trace_id,
            exc.failure.stage,
            exc.failure.error_type,
        )
    return _error_response(exc.status_code, exc.code, exc.public_message, {}, trace_id=trace_id)


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
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    for kind in DATABASE_UNAVAILABLE:
        app.add_exception_handler(kind, database_unavailable_handler)
