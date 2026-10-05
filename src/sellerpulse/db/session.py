"""Database engine and session management for SellerPulse."""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from sellerpulse.config import get_settings
from sellerpulse.db.models import Base

logger = logging.getLogger(__name__)

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def get_engine(db_url: str | None = None, **engine_kwargs: Any) -> Engine:
    """Create or return existing SQLAlchemy engine configured for SQLite WAL mode."""
    global _ENGINE
    if _ENGINE is not None and db_url is None:
        return _ENGINE

    settings = get_settings()
    url = db_url or settings.DATABASE_URL

    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        settings.resolve_db_path()

    engine = create_engine(
        url,
        connect_args=connect_args,
        echo=False,
        future=True,
        **engine_kwargs,
    )

    # Enable SQLite WAL mode and foreign key constraints on connection
    if url.startswith("sqlite") and ":memory:" not in url:

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
            except Exception as exc:
                logger.warning("Failed to execute SQLite PRAGMAs: %s", exc)
            finally:
                cursor.close()

    if db_url is None:
        _ENGINE = engine
    return engine


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Retrieve session factory instance."""
    global _SESSION_FACTORY
    target_engine = engine or get_engine()
    if _SESSION_FACTORY is None or engine is not None:
        factory = sessionmaker(
            bind=target_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        if engine is None:
            _SESSION_FACTORY = factory
        return factory
    return _SESSION_FACTORY


@contextmanager
def get_db_context(engine: Engine | None = None) -> Generator[Session, None, None]:
    """Context manager for database sessions with automatic commit/rollback."""
    factory = get_session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for yielding transactional database sessions."""
    with get_db_context() as session:
        yield session


def init_db(engine: Engine | None = None) -> None:
    """Initialize database schema by creating all tables defined in Base."""
    target_engine = engine or get_engine()
    logger.info("Initializing database tables...")
    Base.metadata.create_all(bind=target_engine)
    logger.info("Database schema initialized successfully.")


def reset_db(engine: Engine | None = None) -> None:
    """Drop all tables and recreate schema (used primarily for test isolation)."""
    target_engine = engine or get_engine()
    Base.metadata.drop_all(bind=target_engine)
    Base.metadata.create_all(bind=target_engine)
