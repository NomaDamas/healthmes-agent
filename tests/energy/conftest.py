"""Fixtures for the cognitive-energy test suite (engine, hourly job, forecast API).

No network, Docker, or credentials: open-wearables rows are canned dicts fed
through an async fake reader (the real ``OwEnergyReader`` is exercised with an
``httpx.MockTransport``), and the store is in-memory sqlite with the full
schema. The shared ``settings`` fixture comes from the top-level
tests/conftest.py.

The "vector" fixtures pin the hand-computed test vector of
tests/energy/test_cognitive_energy.py (docs/PLAN.md §3):

- 7 internal sleep scores of 80            -> sleep-debt index 20, severity 0.2
- Garmin stress 40 on the day              -> time-weighted 40, severity 0.4
- nocturnal RMSSD 45 vs baseline 50 (sd 4) -> z -1.25, severity 0.5
- Garmin body battery 80                   -> charge 0.8
- one 14:00-14:30 meeting                  -> severity 0.45 for the 14:00 window
- 6 social launches in the 13:00 bucket    -> severity 0.5 for the 14:00 window
"""

import datetime as dt
import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from healthmes.engine.cognitive_energy import CognitiveEnergyEngine, OwRows
from healthmes.store import (
    AppUsageSample,
    Base,
    CalendarEventMirror,
    CalendarSource,
    create_db_engine,
)

# The frozen "wall clock" of the vector: 14:23 UTC -> current window is 14:00.
VECTOR_NOW = dt.datetime(2026, 7, 9, 14, 23, tzinfo=dt.UTC)
VECTOR_DAY = dt.date(2026, 7, 9)


@pytest.fixture
def vector_now() -> dt.datetime:
    return VECTOR_NOW


@pytest.fixture
def vector_day() -> dt.date:
    return VECTOR_DAY


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
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Seeding/asserting session (separate from the engine's own sessions)."""
    with session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Canned open-wearables rows (vendor REST v1 row shapes)
# ---------------------------------------------------------------------------


@pytest.fixture
def make_score_row() -> Callable[..., dict]:
    """Factory for a HealthScoreResponse-shaped row (routes/v1/health_scores.py)."""

    def make(
        category: str,
        provider: str,
        recorded_at: str,
        value: float | None,
        *,
        qualifier: str | None = None,
        components: dict | None = None,
    ) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "category": category,
            "provider": provider,
            "value": value,
            "qualifier": qualifier,
            "recorded_at": recorded_at,
            "zone_offset": None,
            "components": components,
            "data_source_id": None,
        }

    return make


@pytest.fixture
def make_sleep_row() -> Callable[..., dict]:
    """Factory for a SleepSummary-shaped row (summaries/sleep)."""

    def make(date_str: str, **fields: object) -> dict:
        return {"date": date_str, "source": {"provider": "oura"}, **fields}

    return make


@pytest.fixture
def full_signal_ow_rows(make_score_row, make_sleep_row) -> OwRows:
    """Vector open-wearables rows: sleep debt 20, stress 40, HRV z -1.25, battery 80."""
    score_rows = [
        make_score_row("sleep", "internal", f"2026-07-{day:02d}T07:00:00+00:00", 80)
        for day in range(3, 10)
    ]
    score_rows.append(make_score_row("stress", "garmin", "2026-07-09T10:00:00+00:00", 40))
    score_rows.append(make_score_row("body_battery", "garmin", "2026-07-09T06:30:00+00:00", 80))
    # RMSSD baseline nights 07-02..07-08: median 50, sample stdev exactly 4;
    # the 07-09 night is 45 -> z = (45 - 50) / 4 = -1.25.
    rmssd = {2: 46, 3: 46, 4: 46, 5: 50, 6: 54, 7: 54, 8: 54, 9: 45}
    sleep_rows = [
        make_sleep_row(f"2026-07-{day:02d}", avg_hrv_rmssd_ms=value)
        for day, value in sorted(rmssd.items())
    ]
    return OwRows(tuple(score_rows), tuple(sleep_rows))


@pytest.fixture
def fake_reader_factory():
    """Factory for an async fake of the engine's open-wearables reader."""

    class FakeReader:
        def __init__(self, rows: OwRows) -> None:
            self.rows = rows
            self.calls: list[dt.date] = []

        async def read(self, as_of: dt.date) -> OwRows:
            self.calls.append(as_of)
            return self.rows

    def make(rows: OwRows | None = None) -> FakeReader:
        return FakeReader(rows if rows is not None else OwRows())

    return make


@pytest.fixture
def energy_engine_factory(settings, session_factory, fake_reader_factory):
    """Build a CognitiveEnergyEngine with injected fakes (no network/env)."""

    def make(
        rows: OwRows | None = None,
        *,
        now: dt.datetime | None = None,
        reader=None,
    ) -> CognitiveEnergyEngine:
        kwargs = {}
        if now is not None:
            kwargs["now_provider"] = lambda: now
        return CognitiveEnergyEngine(
            settings,
            session_factory=session_factory,
            ow_reader=reader if reader is not None else fake_reader_factory(rows),
            **kwargs,
        )

    return make


# ---------------------------------------------------------------------------
# Store seeding
# ---------------------------------------------------------------------------


@pytest.fixture
def seed_calendar_event(session_factory) -> Callable[..., None]:
    def seed(start: dt.datetime, end: dt.datetime, summary: str = "Team sync") -> None:
        with session_factory() as session:
            session.add(
                CalendarEventMirror(
                    external_id=uuid.uuid4().hex,
                    calendar_source=CalendarSource.GOOGLE,
                    summary=summary,
                    start_at=start,
                    end_at=end,
                )
            )
            session.commit()

    return seed


@pytest.fixture
def seed_usage_bucket(session_factory) -> Callable[..., None]:
    def seed(
        bucket_start: dt.datetime,
        *,
        app_package: str = "com.instagram.android",
        launches: int = 6,
        category: str | None = "social",
        foreground_seconds: int = 600,
        device_id: str = "pixel-1",
    ) -> None:
        with session_factory() as session:
            session.add(
                AppUsageSample(
                    device_id=device_id,
                    bucket_start=bucket_start,
                    app_package=app_package,
                    foreground_seconds=foreground_seconds,
                    launches=launches,
                    category=category,
                )
            )
            session.commit()

    return seed


@pytest.fixture
def seed_vector_store(seed_calendar_event, seed_usage_bucket) -> Callable[[], None]:
    """Vector store rows: one 14:00-14:30 meeting + 6 social launches at 13:00."""

    def seed() -> None:
        seed_calendar_event(
            dt.datetime(2026, 7, 9, 14, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 9, 14, 30, tzinfo=dt.UTC),
        )
        seed_usage_bucket(dt.datetime(2026, 7, 9, 13, 0, tzinfo=dt.UTC))

    return seed
