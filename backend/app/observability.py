"""Request correlation and safe registration/login failure diagnostics."""

from dataclasses import dataclass
import logging
from pathlib import Path
import traceback
from typing import Literal
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import Request
import psycopg
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent

ErrorCause = Literal["database_unavailable", "database_conflict", "database_error", "unexpected"]


def error_types(exc: BaseException) -> str:
    """SQL·입력값 없이 예외 클래스와 SQLSTATE만 기록한다."""
    names = [type(exc).__name__]
    cause = getattr(exc, "orig", None)
    if cause is not None:
        names.append(type(cause).__name__)
        sqlstate = getattr(cause, "sqlstate", None)
        if isinstance(sqlstate, str):
            names.append(sqlstate)
    return "/".join(names)


def _cause(exc: BaseException) -> ErrorCause:
    if isinstance(exc, (OperationalError, InterfaceError, PoolTimeoutError, psycopg.OperationalError)):
        return "database_unavailable"
    if isinstance(exc, (IntegrityError, psycopg.IntegrityError)):
        return "database_conflict"
    if isinstance(exc, (SQLAlchemyError, psycopg.Error)):
        return "database_error"
    return "unexpected"


def _where(exc: BaseException) -> str:
    """앱 코드에서 예외가 지나간 가장 안쪽 위치(파일:줄)만 남긴다. 값과 메시지는 담지 않는다."""
    found = "unknown"
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            relative = Path(frame.filename).resolve().relative_to(_APP_DIR)
        except ValueError:
            continue
        found = f"{relative.as_posix()}:{frame.lineno}"
    return found


@dataclass(frozen=True, slots=True)
class SafeError:
    """예외 원문·traceback 대신 남기는 원인 분류. SQL 본문·파라미터·질문·토큰·연결 문자열을 담지 않는다."""

    cause: ErrorCause
    error_type: str
    where: str

    @classmethod
    def of(cls, exc: BaseException) -> "SafeError":
        return cls(cause=_cause(exc), error_type=error_types(exc), where=_where(exc))

    def __str__(self) -> str:
        return f"cause={self.cause}, error_type={self.error_type}, where={self.where}"


class SafeExceptionLogFilter(logging.Filter):
    """서버가 기록하는 미처리 예외의 원문·traceback을 원인 분류로 바꾼다.

    Uvicorn은 요청 처리 중 놓친 예외를 `exc_info`와 함께 기록하며, DB 예외의 원문에는
    SQL 본문과 파라미터가 들어 있다. 고정 메시지는 유지하고 예외 정보만 교체한다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.exc_info:
            return True
        exc = record.exc_info[1]
        message = record.getMessage().rstrip()
        if exc is not None:
            message = f"{message} {SafeError.of(exc)}"
        record.msg = message
        record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def protect_exception_logs() -> None:
    """Uvicorn 서버 로그의 미처리 예외 기록에 안전 필터를 한 번만 붙인다."""
    target = logging.getLogger("uvicorn.error")
    if not any(isinstance(item, SafeExceptionLogFilter) for item in target.filters):
        target.addFilter(SafeExceptionLogFilter())


class GoogleCallbackQueryFilter(logging.Filter):
    """Remove Google login queries while preserving Uvicorn's structured access log arguments."""

    _paths = {
        "/api/auth/google/callback",
        "/api/auth/google/callback/",
        "/api/auth/google/start",
        "/api/auth/google/start/",
    }

    @classmethod
    def _safe_url(cls, value: object) -> object:
        try:
            parts = urlsplit(str(value))
        except ValueError:
            return "[invalid URL]"
        if unquote(parts.path) not in cls._paths:
            return value
        return urlunsplit(parts._replace(query="", fragment=""))

    def filter(self, record: logging.LogRecord) -> bool:
        position = None
        if record.name == "uvicorn.access" and record.msg == '%s - "%s %s HTTP/%s" %d':
            position = 2
        elif record.name == "httpx" and record.msg == 'HTTP Request: %s %s "%s %d %s"':
            position = 1
        if position is None or not isinstance(record.args, tuple) or len(record.args) != 5:
            # Drop unfamiliar callback formats rather than risk logging their credentials.
            return not any(path in unquote(record.getMessage()) for path in self._paths)
        values = list(record.args)
        values[position] = self._safe_url(values[position])
        record.args = tuple(values)
        return True


def protect_google_callback_logs() -> None:
    """Cover server access logs and HTTPX request summaries, including local TestClient runs."""
    for name in ("uvicorn.access", "httpx"):
        target = logging.getLogger(name)
        if not any(isinstance(item, GoogleCallbackQueryFilter) for item in target.filters):
            target.addFilter(GoogleCallbackQueryFilter())


FailureStage = Literal[
    "session",
    "password_hash",
    "password_verify",
    "email_lookup",
    "user_insert",
    "user_lock",
    "token_issue",
    "refresh_session",
    "commit",
    "rollback",
    "registration",
    "login",
    "cookie",
    "compensation",
    "compensation_rollback",
    "google_settings",
    "google_discovery",
    "google_transaction",
    "google_code_exchange",
    "google_jwks",
    "google_identity_lookup",
    "google_identity_insert",
]


@dataclass(frozen=True, slots=True)
class FailureDetails:
    """예외 원문 대신 실패 단계와 클래스 이름만 전달한다."""

    stage: FailureStage
    error_type: str

    @classmethod
    def from_exception(cls, stage: FailureStage, exc: Exception) -> "FailureDetails":
        return cls(stage=stage, error_type=type(exc).__name__)


def request_trace_id(request: Request) -> str:
    """클라이언트 헤더가 아닌 서버가 생성한 ID를 재사용한다."""
    trace_id = getattr(request.state, "trace_id", None)
    if not isinstance(trace_id, str):
        trace_id = str(uuid4())
        request.state.trace_id = trace_id
    return trace_id


class RegistrationTraceMiddleware:
    """Assign a server trace ID and record supported authentication requests before validation."""

    _operations = {
        ("POST", "/api/auth/register"): "registration",
        ("POST", "/api/auth/login"): "login",
        ("GET", "/api/auth/google/callback"): "google_callback",
        ("GET", "/api/auth/csrf"): "csrf",
        ("POST", "/api/auth/refresh"): "refresh",
        ("POST", "/api/auth/logout"): "logout",
    }

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            operation = self._operations.get((scope["method"], scope["path"]))
            if operation is not None:
                trace_id = str(uuid4())
                scope.setdefault("state", {})["trace_id"] = trace_id
                logger.info("event=%s_received trace_id=%s", operation, trace_id)
        await self.app(scope, receive, send)


def google_callback_verification_failed(request: Request) -> None:
    """Correlate rejected callbacks without recording credentials or provider error text."""
    logger.warning("event=google_callback_verification_failed trace_id=%s", request_trace_id(request))


def google_login_failure(
    request: Request, reason: Literal["cancelled", "invalid_request", "temporarily_unavailable"]
) -> None:
    """Record only the fixed public classification, linked to the server-generated trace ID."""
    logger.warning("event=google_login_failure trace_id=%s reason=%s", request_trace_id(request), reason)


def registration_saved(request: Request) -> None:
    """커밋 성공 후 사용자 정보 없이 저장 완료를 기록한다."""
    logger.info("event=registration_saved trace_id=%s", request_trace_id(request))


def registration_cancelled(request: Request) -> None:
    """보상 커밋이 완료된 경우에만 취소 성공을 기록한다."""
    logger.info("event=registration_cancelled trace_id=%s", request_trace_id(request))


def registration_compensation_failed(request: Request, failure: FailureDetails) -> None:
    """쿠키 오류와 구분해서 보상 실패를 같은 요청 ID로 기록한다."""
    logger.error(
        "event=registration_compensation_failed trace_id=%s stage=%s error_type=%s",
        request_trace_id(request),
        failure.stage,
        failure.error_type,
    )


def login_saved(request: Request) -> None:
    """로그인 세션의 저장 성공을 개인 정보 없이 기록한다."""
    logger.info("event=login_saved trace_id=%s", request_trace_id(request))


def login_cancelled(request: Request) -> None:
    """이번 로그인 세션의 취소가 저장된 뒤에만 기록한다."""
    logger.info("event=login_cancelled trace_id=%s", request_trace_id(request))


def login_compensation_failed(request: Request, failure: FailureDetails) -> None:
    """취소 실패의 안전한 오류 종류만 원래 요청의 추적 ID와 함께 기록한다."""
    logger.error(
        "event=login_compensation_failed trace_id=%s stage=%s error_type=%s",
        request_trace_id(request),
        failure.stage,
        failure.error_type,
    )
