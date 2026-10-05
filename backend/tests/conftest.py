"""Reusable guarded ORM integration fixture for feature model tests."""

import pytest
from sqlalchemy import Integer, String, create_engine, inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, psycopg_url
from app.db_init import initialize_with_engine
from db_guard import guarded_live_url


class OrmProbe(Base):
    __tablename__ = "b71_orm_test_probe"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(50))


@pytest.fixture
def orm_test_connection():
    """A verified test connection whose DDL, savepoints and data all roll back."""
    url = guarded_live_url()
    engine = create_engine(psycopg_url(url), connect_args={"connect_timeout": 3})
    existed = inspect(engine).has_table(OrmProbe.__tablename__)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        initialize_with_engine(connection)
        yield connection
    finally:
        transaction.rollback()
        connection.close()
        if not existed:
            assert not inspect(engine).has_table(OrmProbe.__tablename__)
        engine.dispose()
