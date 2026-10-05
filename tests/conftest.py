"""Pytest fixtures for SellerPulse test suite."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sellerpulse.db.models import Base
from sellerpulse.etl.load import run_etl_pipeline
from sellerpulse.ingestion.synthetic import SyntheticConnector


@pytest.fixture
def db_engine():
    """Create in-memory SQLite engine with foreign key support."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session(db_engine):
    """Provide transactional session scoped to test."""
    session_factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def populated_session(db_session):
    """Populate database with a deterministic synthetic dataset for analytics testing."""
    connector = SyntheticConnector(seed=123, days=90, total_skus=30, target_orders=150)
    run_etl_pipeline(connector, session=db_session)
    return db_session
