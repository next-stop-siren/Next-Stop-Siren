"""Small local API for checking the application and its database dependency."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import readiness
from app.api import chat, conversations, cookie_sessions, google_auth, me
from app.api.auth import router as auth_router
from app.api.chat_events import ChatEvents
from app.api.errors import register_error_handlers
from app.db import get_session_factory
from app.integrations.provider import get_provider
from app.observability import RegistrationTraceMiddleware, protect_exception_logs, protect_google_callback_logs
from app.security.login_limits import LoginFailureLimiter
from app.security.registration_limits import RegistrationLimiter
from app.security.settings import (
    AuthSettings,
    AuthSettingsError,
    GoogleSettings,
    RegistrationLimitSettings,
    get_auth_settings,
)
from app.services import quota
from app.services.chat_runner import ChatJobWorker

logger = logging.getLogger(__name__)
# Seconds a shutdown waits for running chat jobs; one cut off is interrupted by recovery after a restart.
WORKER_STOP_SECONDS = 35.0


def chat_worker_enabled() -> bool:
    """`CHAT_WORKER=off` keeps this process from running chat jobs; tests use it."""
    return os.environ.get("CHAT_WORKER", "on").strip().lower() != "off"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Run the member chat queue's worker while the app serves; waiting jobs resume after a restart."""
    events: ChatEvents = application.state.chat_events
    worker = None
    if not chat_worker_enabled():
        application.state.chat_worker_issue = "disabled"
    # A second start of the same app, as two test clients do, shares the first one's worker.
    elif not events.attached:
        try:
            sessions = get_session_factory()
        except RuntimeError:
            logger.warning("Chat worker not started: the database is not configured")
        else:
            worker = ChatJobWorker(
                sessions, get_provider(), quota.MemberQuota(), notify=events.notify, on_delta=events.delta
            )
            events.attach(sessions, worker)
            worker.start()
            application.state.chat_worker = worker
    try:
        yield
    finally:
        if worker is not None:
            events.detach()
            await run_in_threadpool(worker.stop, timeout=WORKER_STOP_SECONDS)


def health() -> dict[str, str]:
    """API 프로세스가 요청에 응답할 수 있는지 확인한다. 의존성은 보지 않는다."""
    return {"status": "ok"}


def _auth_status(application: FastAPI) -> str:
    state = application.state
    if state.auth_settings is None or state.registration_limit_settings is None:
        return "not_configured"
    return readiness.OK


def _worker_status(application: FastAPI) -> str:
    worker: ChatJobWorker | None = application.state.chat_worker
    if worker is None:
        return str(application.state.chat_worker_issue)
    return worker.status(stale_after=readiness.WORKER_STALE_SECONDS)


def ready(request: Request) -> dict[str, str] | JSONResponse:
    """DB·인증 설정·제품 스키마·대기열 실행기가 모두 준비돼야 준비 완료로 응답한다 (#72)."""
    application = request.app
    result = readiness.evaluate(auth=_auth_status(application), worker=_worker_status(application))
    readiness.log_change(application.state.last_readiness, result)
    application.state.last_readiness = result
    if result.ready:
        return {"status": "ready"}
    if result.database != readiness.OK:
        # 기존 화면·스크립트가 쓰는 코드와 문구를 유지한다.
        code, message = "database_unavailable", "데이터베이스에 연결할 수 없습니다."
    else:
        code, message = "not_ready", "서비스가 아직 요청을 받을 준비가 되지 않았습니다."
    return JSONResponse(status_code=503, content={"code": code, "message": message, "checks": result.checks()})


def create_app(
    auth_settings: AuthSettings | None = None,
    google_settings: GoogleSettings | None = None,
    registration_limit_settings: RegistrationLimitSettings | None = None,
) -> FastAPI:
    """앱 생성 시 인증 설정을 고정하며, 잘못된 설정은 인증 요청을 차단한다."""
    # 기존 로그 설정이 없으면 요청·저장 성공의 INFO 기록도 출력한다.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    protect_google_callback_logs()
    protect_exception_logs()
    if auth_settings is None:
        try:
            auth_settings = get_auth_settings()
        except AuthSettingsError:
            # 인증 설정이 없어도 기존 상태 확인 경로는 사용할 수 있다.
            auth_settings = None
    if google_settings is None and auth_settings is not None:
        try:
            google_settings = GoogleSettings.from_env(auth_settings=auth_settings)
        except AuthSettingsError:
            # Google 설정이 없어도 로컬 가입·로그인과 상태 확인 경로는 유지한다.
            google_settings = None
    if registration_limit_settings is None:
        try:
            registration_limit_settings = RegistrationLimitSettings.from_env()
        except AuthSettingsError as exc:
            # 잘못된 설정을 기본값이나 무제한으로 대체하지 않고 가입만 차단한다.
            logger.error("Registration limits unavailable: %s", exc)
    application = FastAPI(title="B7-1 API", lifespan=lifespan)
    application.state.auth_settings = auth_settings
    application.state.google_settings = google_settings
    application.state.registration_limit_settings = registration_limit_settings
    application.state.registration_limiter = (
        RegistrationLimiter(registration_limit_settings) if registration_limit_settings is not None else None
    )
    application.state.login_failure_limiter = LoginFailureLimiter()
    application.state.chat_events = ChatEvents()
    # The lifespan sets the worker, or why there is none; readiness reads both.
    application.state.chat_worker = None
    application.state.chat_worker_issue = "not_started"
    application.state.last_readiness = None
    register_error_handlers(application)
    application.add_middleware(RegistrationTraceMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(auth_settings.allowed_origins) if auth_settings else [],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
        expose_headers=["Retry-After"],
    )
    application.add_api_route("/api/health", health, methods=["GET"])
    application.add_api_route("/api/ready", ready, methods=["GET"], response_model=None)
    application.include_router(auth_router)
    application.include_router(cookie_sessions.router)
    application.include_router(google_auth.router)
    application.include_router(me.router)
    application.include_router(conversations.router)
    application.include_router(chat.router)
    return application


app = create_app()
