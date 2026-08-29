import hashlib
import hmac
import json
import os
from typing import Any, Dict, Generator

import pytest
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

TEST_SECRET = "pytest-only-secret-not-for-production"
TEST_WEBHOOK_SECRET = "pytest-webhook-secret-not-for-production"
TEST_GROQ_KEY = "pytest-mock-groq-key"

os.environ.setdefault("BLINDLOG_SECRET", TEST_SECRET)
os.environ.setdefault("WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
os.environ.setdefault("GROQ_API_KEY", TEST_GROQ_KEY)
os.environ.pop("BLINDLOG_DEBUG", None)

from recover_net.db.session import init_db  # noqa: E402
from recover_net.core.security import clear_logger_cache  # noqa: E402


def sign_payload(
    payload: Any, secret: str = TEST_WEBHOOK_SECRET
) -> tuple[bytes, Dict[str, str]]:
    """Helper to compute exact raw bytes and valid HMAC-SHA256 headers for testing webhooks."""
    if isinstance(payload, bytes):
        raw_bytes = payload
    elif isinstance(payload, str):
        raw_bytes = payload.encode("utf-8")
    else:
        raw_bytes = json.dumps(payload).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": f"sha256={sig}",
    }
    return raw_bytes, headers


@pytest.fixture(autouse=True)
def _configure_blindlog(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("BLINDLOG_SECRET", TEST_SECRET)
    monkeypatch.setenv("WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
    monkeypatch.setenv("GROQ_API_KEY", TEST_GROQ_KEY)
    monkeypatch.delenv("BLINDLOG_DEBUG", raising=False)
    clear_logger_cache()
    yield
    clear_logger_cache()


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection: Any, _connection_record: Any) -> None:
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
