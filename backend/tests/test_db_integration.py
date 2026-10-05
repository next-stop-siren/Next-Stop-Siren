"""Read-only integration probe of the dedicated local test database."""

import psycopg
import pytest

from db_guard import guarded_live_url


@pytest.mark.db
def test_dedicated_database_select_one():
    url = guarded_live_url()  # must run before psycopg.connect
    with psycopg.connect(url, connect_timeout=2, options="-c statement_timeout=2000") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
