"""
Shared pytest fixtures.

Sets BLINDLOG_SECRET before application imports so hashing never sees an empty
key. Each test gets its own in-memory SQLite session wrapped in a transaction
that is rolled back at teardown.
"""

import os

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault("BLINDLOG_SECRET", "pytest-only-secret-not-for-production")
os.environ.pop("BLINDLOG_DEBUG", None)

from database import Base, init_db  # noqa: E402
from security import clear_logger_cache  # noqa: E402

TEST_SECRET = "pytest-only-secret-not-for-production"


@pytest.fixture(autouse=True)
def _configure_blindlog(monkeypatch):
    monkeypatch.setenv("BLINDLOG_SECRET", TEST_SECRET)
    monkeypatch.delenv("BLINDLOG_DEBUG", raising=False)
    clear_logger_cache()
    yield
    clear_logger_cache()


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    init_db(engine)
    connection = engine.connect()
    transaction = connection.begin()
    SessionFactory = sessionmaker(bind=connection, expire_on_commit=False)
    session = SessionFactory()
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()
