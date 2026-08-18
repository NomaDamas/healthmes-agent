from collections.abc import Iterator

import pytest
from freezegun import freeze_time
from sqlalchemy.orm import Session, sessionmaker

from healthmes.store import Base, create_db_engine


@pytest.fixture(autouse=True)
def stable_activity_wall_clock(request) -> Iterator[None]:
    """Keep fixed 2026 activity fixtures inside the default retention window."""
    if request.node.path.name == "test_control_postgres.py":
        # This module builds FastAPI apps inside tests and performs first
        # requests on worker threads, which cannot run under freezegun.
        yield
        return
    with freeze_time("2026-08-14 12:00:00", tick=True, real_asyncio=True):
        yield


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        yield db
    engine.dispose()
