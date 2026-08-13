"""Fixtures for the engine test suite: in-memory store + fire factory.

No network, no Docker, no real credentials: the store runs on in-memory
sqlite (same ``create_db_engine`` safety settings as production), health
signals come from in-test fakes, and webhook pushes go to recording fakes or
an httpx.MockTransport. The shared ``settings`` fixture comes from the
top-level tests/conftest.py.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.google import google_token_path
from healthmes.calendars.state import FileSyncHealthStore
from healthmes.engine.rules import TriggerFire
from healthmes.store import Base, create_db_engine
from healthmes.store.enums import CalendarSource

GOOGLE_ACCOUNT_GENERATION = "a" * 32
CALDAV_ACCOUNT_GENERATION = "b" * 32


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
def connect_calendar_sources(
    settings,
) -> Callable[..., dict[CalendarSource, str]]:
    """Create real credential + first-sync state for selected test sources."""

    defaults = {
        CalendarSource.GOOGLE: GOOGLE_ACCOUNT_GENERATION,
        CalendarSource.CALDAV: CALDAV_ACCOUNT_GENERATION,
    }

    def connect(
        *sources: CalendarSource,
        generations: dict[CalendarSource, str] | None = None,
        synced_at: datetime | None = None,
    ) -> dict[CalendarSource, str]:
        selected = sources or tuple(CalendarSource)
        resolved = {
            source: (generations or {}).get(source, defaults[source])
            for source in selected
        }
        for source, generation in resolved.items():
            if source is CalendarSource.GOOGLE:
                creds.write_owner_only_json(
                    google_token_path(settings.data_dir),
                    {
                        "type": "authorized_user",
                        "refresh_token": "fake-refresh",
                        "client_id": "test.apps.googleusercontent.com",
                        "client_secret": "fake-secret",
                        creds.GOOGLE_ACCOUNT_GENERATION_KEY: generation,
                    },
                )
            else:
                creds.save_caldav_credentials(
                    settings.data_dir,
                    username="owner@example.com",
                    app_password="test-app-password",
                    url=settings.caldav_url,
                    account_generation=generation,
                )
            FileSyncHealthStore.for_data_dir(
                settings.data_dir
            ).record_success(
                source,
                synced_at or datetime(2026, 7, 1, tzinfo=UTC),
                event_count=0,
                account_generation=generation,
            )
        return resolved

    return connect


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
