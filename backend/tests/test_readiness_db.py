"""Readiness schema check on the guarded test database, in a temporary schema (#72)."""

from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.schema import CreateSchema, DropSchema

from app import readiness
from app.db import Base, create_db_engine
from db_guard import guarded_live_url

pytestmark = pytest.mark.db


@pytest.fixture
def product_schema():
    """A temporary schema holding the product tables; yields its name and a way to connect like readiness."""
    url = guarded_live_url()
    engine = create_db_engine(url)
    schema = f"ready_test_{uuid4().hex}"
    expected = readiness.expected_columns()
    tables = [Base.metadata.tables[name] for name in expected]
    scoped = engine.execution_options(schema_translate_map={None: schema})
    created = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        Base.metadata.create_all(scoped, tables=tables)

        def connect() -> psycopg.Connection:
            options = f"{readiness.READ_ONLY_OPTIONS} -c search_path={schema}"
            return psycopg.connect(url, connect_timeout=2, options=options)

        yield schema, engine, connect
    finally:
        try:
            if created:
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            engine.dispose()


def compare(connect) -> tuple[str, tuple[str, ...]]:
    expected = readiness.expected_columns()
    with connect() as connection:
        return readiness.compare_schema(expected, readiness.read_columns(connection, list(expected)))


def test_initialized_schema_is_ready(product_schema):
    _, _, connect = product_schema
    assert compare(connect) == ("ok", ())


def test_missing_table_and_column_are_reported(product_schema):
    schema, engine, connect = product_schema
    with engine.begin() as connection:
        connection.execute(text(f'ALTER TABLE "{schema}".chat_jobs DROP COLUMN run_token'))
    assert compare(connect) == ("missing_columns", ("chat_jobs.run_token",))
    with engine.begin() as connection:
        connection.execute(text(f'DROP TABLE "{schema}".chat_conversation_holds'))
    assert compare(connect) == ("missing_tables", ("chat_conversation_holds",))


def test_check_connection_cannot_change_data_or_schema(product_schema):
    schema, engine, connect = product_schema
    with engine.connect() as connection:
        before = connection.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :schema"), {"schema": schema}
        )
    with connect() as connection, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        connection.execute("CREATE TABLE readiness_probe (id int)")
    with connect() as connection, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        connection.execute("DELETE FROM users")
    with engine.connect() as connection:
        after = connection.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :schema"), {"schema": schema}
        )
    assert after == before
