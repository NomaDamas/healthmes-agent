"""Fixtures for the REST API test suite.

A file-backed sqlite database in ``tmp_path`` gives each test full isolation
with real (per-connection) sessions; the app is built through the real factory
plus :func:`healthmes.api.include_all`, and ``get_session`` is overridden onto
the test engine. No network, Docker, or credentials — the open-wearables
client is injected with an ``httpx.MockTransport``.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from healthmes.api import include_all
from healthmes.app import create_app
from healthmes.config import Settings
from healthmes.mcp_server.ow_client import OWClient
from healthmes.store import Base, create_db_engine
from healthmes.store.session import get_session


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    """File-backed sqlite engine with the full schema created."""
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path}/healthmes-api-test.db")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Seeding/asserting session (separate from the request-scoped ones)."""
    with session_factory() as session:
        yield session


@pytest.fixture
def app(settings: Settings, session_factory: sessionmaker[Session]) -> FastAPI:
    """App with all API routers mounted and ``get_session`` bound to the test db."""
    application = create_app(settings)
    include_all(application)

    def _override_get_session() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_session] = _override_get_session
    return application


# Helpers are exposed as fixtures returning callables because importlib test
# mode (no __init__.py) does not support importing from conftest directly.


@pytest.fixture
def ow_client_factory():
    """Factory building the shared OWClient over an ``httpx.MockTransport``."""

    def make(handler) -> OWClient:
        return OWClient(
            base_url="http://ow.test",
            api_key="test-key",
            transport=httpx.MockTransport(handler),
        )

    return make


@pytest.fixture
def parse_utc():
    """Parse an API datetime string; naive values (sqlite) are UTC by contract."""

    def _parse(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    return _parse
