"""Unit checks for URL selection and request transaction ownership."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app import db
from app import db_init


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_psycopg_url_normalizes_standard_schemes(scheme):
    url = db.psycopg_url(f"{scheme}://user:pass@127.0.0.1:55433/name")
    assert url.startswith("postgresql+psycopg://")
    assert "user:pass@127.0.0.1:55433/name" in url


def test_other_driver_is_refused():
    with pytest.raises(ValueError):
        db.psycopg_url("postgresql+psycopg2://user:pass@localhost/name")


def test_product_registry_is_repeatable_without_test_models():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from app.db import Base; from app.models import register_models; "
            "register_models(); register_models(); print(json.dumps(sorted(Base.metadata.tables)))",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == ["auth_identities", "refresh_sessions", "users"]


def test_empty_application_registry_needs_no_connection(monkeypatch):
    class EmptyMetadata:
        tables = {}

    class EmptyBase:
        metadata = EmptyMetadata()

    monkeypatch.setattr(db_init, "Base", EmptyBase)
    assert db_init.initialize_schema("postgresql://unused:unused@127.0.0.1:55432/unused") == 0


def test_request_session_does_not_auto_commit_and_closes(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE rows (value INTEGER)"))
    sessions = []

    class CountingSession(Session):
        closed_count = 0

        def close(self):
            self.closed_count += 1
            super().close()

    def factory():
        session = CountingSession(engine)
        sessions.append(session)
        return session

    monkeypatch.setattr(db, "_session_factory", factory)
    dependency = db.get_session()
    session = next(dependency)
    session.execute(text("INSERT INTO rows VALUES (1)"))
    with pytest.raises(StopIteration):
        next(dependency)
    assert not sessions[0].in_transaction()
    assert sessions[0].closed_count == 1
    with Session(engine) as verification:
        assert verification.scalar(text("SELECT count(*) FROM rows")) == 0
    engine.dispose()


def test_explicit_commit_persists(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE rows (value INTEGER)"))
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
    dependency = db.get_session()
    session = next(dependency)
    session.execute(text("INSERT INTO rows VALUES (2)"))
    session.commit()
    with pytest.raises(StopIteration):
        next(dependency)
    with Session(engine) as verification:
        assert verification.scalar(text("SELECT count(*) FROM rows")) == 1
    engine.dispose()
