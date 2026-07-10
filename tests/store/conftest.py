"""Fixtures for the store test suite: in-memory sqlite engine + sessions.

Everything runs on sqlite (no network/Docker); the engine comes from the real
``create_db_engine`` so its sqlite safety settings (StaticPool, foreign-keys
pragma) are exercised by every test.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from healthmes.store import Base, create_db_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory sqlite engine with the full schema created."""
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session
