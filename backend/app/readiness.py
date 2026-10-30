"""What `GET /api/ready` checks before it says the service can take real requests (#72).

`GET /api/health` only says the process answers. Readiness also needs the database,
the authentication settings, the registered product tables and the chat job worker:

- database: a `SELECT 1` within 2 seconds on the configured `DATABASE_URL`.
- auth: the authentication settings and registration limits loaded when the app was
  created. Google settings are optional and not checked.
- schema: every table and column of the registered product models exists in the
  current schema. Types, constraints and indexes are not compared, and there is no
  schema version yet (#75 chooses the migration tool).
- worker: this process's `ChatJobWorker` is polling. `CHAT_WORKER=off` is test-only,
  so a disabled worker is not ready.

The check never changes data or schema: its connection runs read-only transactions
and reads `information_schema` only. Responses carry fixed status words; the server
log adds missing table or column names and a `SafeError` classification, never an
exception's text, a key, a connection string or SQL.
"""

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache

import psycopg
from sqlalchemy import Table

from app.db import Base
from app.models import register_models
from app.observability import SafeError

logger = logging.getLogger(__name__)

OK = "ok"
# Every transaction of the check's connection is read-only, so it cannot change data or schema.
READ_ONLY_OPTIONS = "-c statement_timeout=2000 -c default_transaction_read_only=on"
# A worker whose last successful poll is older than this is stalled (it polls every second).
WORKER_STALE_SECONDS = 30.0

_COLUMNS = (
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = ANY(%s)"
)


@cache
def expected_columns() -> Mapping[str, frozenset[str]]:
    """The columns of each registered product table; models defined elsewhere, such as test probes, are skipped."""
    register_models()
    tables = [
        mapper.local_table for mapper in Base.registry.mappers if mapper.class_.__module__.startswith("app.models.")
    ]
    return {
        table.name: frozenset(column.name for column in table.columns) for table in tables if isinstance(table, Table)
    }


def compare_schema(
    expected: Mapping[str, frozenset[str]], found: Mapping[str, set[str]]
) -> tuple[str, tuple[str, ...]]:
    """`ok`, `missing_tables` or `missing_columns`, with the missing names for the log."""
    tables = tuple(sorted(name for name in expected if name not in found))
    if tables:
        return "missing_tables", tables
    columns = tuple(sorted(f"{name}.{column}" for name, wanted in expected.items() for column in wanted - found[name]))
    if columns:
        return "missing_columns", columns
    return OK, ()


@dataclass(frozen=True)
class Readiness:
    database: str
    auth: str
    schema: str
    worker: str
    # Missing table or table.column names and the database failure, for the server log only.
    missing: tuple[str, ...] = field(default=(), compare=False)
    error: SafeError | None = field(default=None, compare=False)

    @property
    def ready(self) -> bool:
        return all(value == OK for value in self.checks().values())

    def checks(self) -> dict[str, str]:
        return {"database": self.database, "auth": self.auth, "schema": self.schema, "worker": self.worker}


def read_columns(connection: psycopg.Connection, tables: Sequence[str]) -> dict[str, set[str]]:
    """The columns of the named tables that exist in the connection's current schema."""
    found: dict[str, set[str]] = {}
    with connection.cursor() as cursor:
        cursor.execute(_COLUMNS, (list(tables),))
        for table, column in cursor.fetchall():
            found.setdefault(table, set()).add(column)
    return found


def check_database() -> tuple[str, str, tuple[str, ...], SafeError | None]:
    """Run `SELECT 1`, then compare the product tables; return database, schema, missing names and failure."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return "not_configured", "unknown", (), None
    expected = expected_columns()
    try:
        with psycopg.connect(database_url, connect_timeout=2, options=READ_ONLY_OPTIONS) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise RuntimeError("Database readiness query returned an unexpected result")
            try:
                found = read_columns(connection, list(expected))
            except psycopg.Error as error:
                return OK, "unknown", (), SafeError.of(error)
    except (psycopg.Error, OSError, RuntimeError) as error:
        return "unavailable", "unknown", (), SafeError.of(error)
    schema, missing = compare_schema(expected, found)
    return OK, schema, missing, None


def evaluate(*, auth: str, worker: str) -> Readiness:
    database, schema, missing, error = check_database()
    return Readiness(database=database, auth=auth, schema=schema, worker=worker, missing=missing, error=error)


def log_change(previous: Readiness | None, current: Readiness) -> None:
    """Log only when the result changes, so a probe every few seconds does not repeat the same line."""
    if current == previous:
        return
    if current.ready:
        if previous is not None:
            logger.info("Service ready again")
        return
    checks = ", ".join(f"{name}={value}" for name, value in current.checks().items())
    details = ""
    if current.missing:
        details += f", missing={','.join(current.missing)}"
    if current.error is not None:
        details += f", {current.error}"
    logger.warning("Service not ready, %s%s", checks, details)
