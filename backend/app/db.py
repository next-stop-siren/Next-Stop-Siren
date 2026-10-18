"""Synchronous SQLAlchemy connection and request session helpers."""

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Import this base in every application ORM model."""


def psycopg_url(url: str) -> str:
    """Use psycopg 3 for standard PostgreSQL URLs without changing credentials."""
    parsed = make_url(url)
    if parsed.drivername in {"postgresql", "postgres"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    elif parsed.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use PostgreSQL with psycopg")
    return parsed.render_as_string(hide_password=False)


def create_db_engine(url: str) -> Engine:
    return create_engine(
        psycopg_url(url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=5000"},
    )


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is missing")
    return url


def make_session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=create_db_engine(url), autoflush=False, expire_on_commit=False)


_session_factory: sessionmaker[Session] | None = None


def get_session_factory() -> sessionmaker[Session]:
    """The factory of request sessions, for work that outlives the request's own session.

    A streamed response body is produced after `get_session` has closed its session,
    so it opens one here and closes it itself.
    """
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(database_url())
    return _session_factory


def get_session() -> Generator[Session, None, None]:
    """Yield a session; callers explicitly commit or roll back their work."""
    with get_session_factory()() as session:
        try:
            yield session
        finally:
            session.rollback()
