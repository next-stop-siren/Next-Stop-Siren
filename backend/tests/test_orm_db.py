"""Real PostgreSQL checks for repeatable schema creation and savepoint isolation."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_init import initialize_with_engine
from conftest import OrmProbe


@pytest.mark.db
def test_repeatable_init_preserves_row_and_session_commit_is_isolated(orm_test_connection):
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as first:
        first.add(OrmProbe(label="retained within outer transaction"))
        first.commit()
    initialize_with_engine(orm_test_connection)
    initialize_with_engine(orm_test_connection)
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as second:
        labels = second.scalars(select(OrmProbe.label)).all()
        assert "retained within outer transaction" in labels
        second.add(OrmProbe(label="rolled back savepoint"))
        second.rollback()
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as third:
        labels = third.scalars(select(OrmProbe.label)).all()
        assert "retained within outer transaction" in labels
        assert "rolled back savepoint" not in labels
