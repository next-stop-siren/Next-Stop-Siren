"""Convert expected API failures into safe, stable public responses."""

from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.errors import ErrorDetail, ErrorResponse

ApiErrorCode = Literal["local_email_exists", "csrf_failed", "temporarily_unavailable"]

_API_ERRORS: dict[ApiErrorCode, tuple[int, str]] = {
    "local_email_exists": (409, "이미 가입된 이메일입니다."),
    "csrf_failed": (403, "요청 출처 또는 CSRF 값을 확인해 주세요."),
    "temporarily_unavailable": (503, "일시적으로 서비스를 이용할 수 없습니다."),
}
_VALIDATION_MESSAGES = {
    "missing": "필수 값입니다.",
    "string_type": "문자열로 입력해 주세요.",
    "extra_forbidden": "허용하지 않은 필드가 포함되어 있습니다.",
    "json_invalid": "올바른 JSON 본문을 보내 주세요.",
}
_LOCATIONS = {"body", "path", "query", "header", "cookie"}


class ApiError(Exception):
    """Expected failures use approved codes rather than arbitrary exception text."""

    def __init__(self, code: ApiErrorCode) -> None:
        self.code = code
        self.status_code, self.public_message = _API_ERRORS[code]
        super().__init__(code)


def _error_response(status_code: int, code: str, message: str, fields: dict[str, str]) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, fields=fields, trace_id=str(uuid4())))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):
        raise exc
    return _error_response(exc.status_code, exc.code, exc.public_message, {})


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    fields: dict[str, str] = {}
    for error in exc.errors():
        location = error.get("loc", ())
        root = location[0] if location and location[0] in _LOCATIONS else "body"
        # Extra field names and nested dictionary keys can contain secrets.
        # Only expose declared registration fields; other failures use a location.
        field = root
        if error.get("type") != "extra_forbidden" and len(location) > 1 and location[1] in {"email", "password"}:
            field = location[1]
        message = _VALIDATION_MESSAGES.get(error.get("type", ""), "입력값을 확인해 주세요.")
        fields.setdefault(field, message)
    return _error_response(422, "validation_error", "요청을 확인해 주세요.", fields)


def register_error_handlers(app: FastAPI) -> None:
    """Install request validation and expected failure handlers only."""
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
