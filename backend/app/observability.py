"""Request correlation and safe registration/login failure diagnostics."""

from dataclasses import dataclass
import logging
from typing import Literal
from uuid import uuid4

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)
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
