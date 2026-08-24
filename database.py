"""
database.py

Database connection, engine configuration, and session management using SQLAlchemy 2.0.
"""

import os
from typing import Generator, Optional

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/recover_net",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


def get_db() -> Generator[Session, None, None]:
    """
    Dependency helper that yields a database session and ensures cleanup.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(engine_instance: Optional[Engine] = None) -> None:
    """
    Create tables defined on Base.metadata.

    Imports models here so Transaction and AuditLog are registered even when
    the caller only imported database.init_db. Also requires BLINDLOG_SECRET
    so a process that boots the schema cannot run without a hashing key.
    """
    from security import require_blindlog_secret

    require_blindlog_secret()

    from models import AuditLog, Transaction  # noqa: F401

    target_engine = engine_instance or engine
    Base.metadata.create_all(bind=target_engine)
