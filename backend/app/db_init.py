"""Create missing registered tables without modifying existing ones."""

from sqlalchemy.engine import Connection, Engine

from app.db import Base, create_db_engine
from app.models import register_models


def initialize_schema(url: str) -> int:
    register_models()
    tables = len(Base.metadata.tables)
    if tables:
        engine = create_db_engine(url)
        try:
            initialize_with_engine(engine)
        finally:
            engine.dispose()
    return tables


def initialize_with_engine(engine: Engine | Connection) -> int:
    """Testable initialization of the registered application metadata."""
    register_models()
    Base.metadata.create_all(engine)
    return len(Base.metadata.tables)
