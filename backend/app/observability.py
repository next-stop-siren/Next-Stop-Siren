"""Request correlation and safe registration failure diagnostics."""

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
    "email_lookup",
    "user_insert",
    "token_issue",
    "refresh_session",
    "commit",
    "rollback",
    "registration",
    "cookie",
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
    """본문을 읽기 전에 가입 요청 수신을 기록한다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/api/auth/register":
            trace_id = str(uuid4())
            scope.setdefault("state", {})["trace_id"] = trace_id
            logger.info("event=registration_received trace_id=%s", trace_id)
        await self.app(scope, receive, send)


def registration_saved(request: Request) -> None:
    """커밋 성공 후 사용자 정보 없이 저장 완료를 기록한다."""
    logger.info("event=registration_saved trace_id=%s", request_trace_id(request))
