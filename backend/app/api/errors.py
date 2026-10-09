"""The common error body of docs/04-reference/api.md for expected failures and invalid input."""

import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

logger = logging.getLogger(__name__)

# Lost or refused connections, statement time limits and pool waits. Other database
# errors, such as a broken constraint or query, are defects and stay 500.
DATABASE_UNAVAILABLE = (OperationalError, InterfaceError, PoolTimeoutError)


class ApiError(Exception):
    """An expected failure with a stable code and a message that is safe to display."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message


def unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "로그인이 필요합니다.")


def not_found() -> ApiError:
    """One answer for a missing resource and for another user's resource."""
    return ApiError(404, "not_found", "찾을 수 없습니다.")


def error_response(
    status_code: int, code: str, message: str, fields: dict[str, str] | None = None, trace_id: str | None = None
) -> JSONResponse:
    error = {"code": code, "message": message, "fields": fields or {}, "trace_id": trace_id or uuid4().hex}
    return JSONResponse(status_code=status_code, content={"error": error})


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # The last location part is the field name; a malformed or missing body has none.
        fields = {}
        for error in exc.errors():
            name = error["loc"][-1]
            fields[name if isinstance(name, str) else "body"] = "값을 확인해 주세요."
        return error_response(422, "validation_error", "요청을 확인해 주세요.", fields)

    def database_unavailable(_request: Request, exc: Exception) -> JSONResponse:
        # The statement and connection details stay in the server log; the response
        # carries only the trace ID that finds them.
        trace_id = uuid4().hex
        logger.error("Database unavailable, trace_id=%s", trace_id, exc_info=exc)
        return error_response(503, "temporarily_unavailable", "잠시 후 다시 시도해 주세요.", trace_id=trace_id)

    for kind in DATABASE_UNAVAILABLE:
        app.add_exception_handler(kind, database_unavailable)
