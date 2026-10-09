"""Small local API for checking the application and its database dependency."""

import os

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import conversations
from app.api.errors import register_error_handlers

app = FastAPI(title="B7-1 API")
register_error_handlers(app)
app.include_router(conversations.router)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Report whether the API process can answer requests."""
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


@app.get("/api/ready", response_model=None)
def ready() -> dict[str, str] | JSONResponse:
    """Report readiness only after a real bounded PostgreSQL query succeeds."""
    try:
        _check_database()
    except (psycopg.Error, OSError, RuntimeError):
        return JSONResponse(
            status_code=503,
            content={"code": "database_unavailable", "message": "데이터베이스에 연결할 수 없습니다."},
        )
    return {"status": "ready"}
