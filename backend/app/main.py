"""Small local API for checking the application and its database dependency."""

import logging
import os

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import conversations, me
from app.api.auth import router as auth_router
from app.api.errors import register_error_handlers
from app.observability import RegistrationTraceMiddleware
from app.security.login_limits import LoginFailureLimiter
from app.security.settings import AuthSettings, AuthSettingsError, get_auth_settings


def health() -> dict[str, str]:
    """API 프로세스가 요청에 응답할 수 있는지 확인한다."""
    return {"status": "ok"}


def _check_database() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing")
    with psycopg.connect(database_url, connect_timeout=2, options="-c statement_timeout=2000") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise RuntimeError("Database readiness query returned an unexpected result")


def ready() -> dict[str, str] | JSONResponse:
    """제한 시간 안에 실제 PostgreSQL 조회가 성공해야 준비 완료로 응답한다."""
    try:
        _check_database()
    except (psycopg.Error, OSError, RuntimeError):
        return JSONResponse(
            status_code=503,
            content={"code": "database_unavailable", "message": "데이터베이스에 연결할 수 없습니다."},
        )
    return {"status": "ready"}


def create_app(auth_settings: AuthSettings | None = None) -> FastAPI:
    """앱 생성 시 인증 설정을 고정하며, 잘못된 설정은 인증 요청을 차단한다."""
    # 기존 로그 설정이 없으면 요청·저장 성공의 INFO 기록도 출력한다.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if auth_settings is None:
        try:
            auth_settings = get_auth_settings()
        except AuthSettingsError:
            # 인증 설정이 없어도 기존 상태 확인 경로는 사용할 수 있다.
            auth_settings = None
    application = FastAPI(title="B7-1 API")
    application.state.auth_settings = auth_settings
    application.state.login_failure_limiter = LoginFailureLimiter()
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
    application.include_router(me.router)
    application.include_router(conversations.router)
    return application


app = create_app()
