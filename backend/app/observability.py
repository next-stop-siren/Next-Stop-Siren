"""Request correlation and safe registration/login failure diagnostics."""

from dataclasses import dataclass
import logging
from typing import Literal
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


class GoogleCallbackQueryFilter(logging.Filter):
    """Remove callback queries while preserving Uvicorn's structured access log arguments."""

    _paths = {"/api/auth/google/callback", "/api/auth/google/callback/"}

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
            return "/api/auth/google/callback" not in unquote(record.getMessage())
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
    """본문을 읽기 전에 가입·로그인 요청 수신을 기록한다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] in {"/api/auth/register", "/api/auth/login"}
        ):
            trace_id = str(uuid4())
            scope.setdefault("state", {})["trace_id"] = trace_id
            operation = "login" if scope["path"] == "/api/auth/login" else "registration"
            logger.info("event=%s_received trace_id=%s", operation, trace_id)
        await self.app(scope, receive, send)


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
