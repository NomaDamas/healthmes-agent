"""Fixtures for the engine test suite: in-memory store + fire factory.

No network, no Docker, no real credentials: the store runs on in-memory
sqlite (same ``create_db_engine`` safety settings as production), health
signals come from in-test fakes, and webhook pushes go to recording fakes or
an httpx.MockTransport. The shared ``settings`` fixture comes from the
top-level tests/conftest.py.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from healthmes.engine.rules import TriggerFire
from healthmes.store import Base, create_db_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory sqlite engine with the full domain schema created."""
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture
def make_fire() -> Callable[..., TriggerFire]:
    """Factory for a minimal, valid TriggerFire."""

    def _make(
        rule_id: str = "test_rule",
        dedup_key: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> TriggerFire:
        return TriggerFire(
            rule_id=rule_id,
            dedup_key=dedup_key if dedup_key is not None else f"{rule_id}:2026-07-09",
            summary="Stress is 85/100, 1.5x the 10-day baseline of 55.",
            proposal="Suggest a short recovery break now.",
            evidence=evidence if evidence is not None else {"recent_value": 85, "ratio": 1.5},
        )

    return _make
