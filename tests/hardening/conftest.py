"""Fixtures for the hardening suite (docs/PLAN.md Phase 3 + section 11).

``seeded_store`` builds a real, file-backed healthmes store the way
production does — alembic ``upgrade head`` (not ``create_all``) onto sqlite —
and seeds representative rows across the domain tables. The restore drill
snapshots/destroys/restores it; a future backup-provider drill can reuse the
same fixture as its source store. No network, Docker, or credentials.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    DecisionKind,
    DecisionRecord,
    FoodLog,
    Task,
    TriggerEvent,
    WeeklyGoal,
    create_db_engine,
    session_scope,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory sqlite engine with the full domain schema created."""
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Same autocommit/autoflush settings as the production factory."""
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@dataclass(frozen=True)
class SeededStore:
    """A live, migrated, seeded store plus everything a drill needs to verify it."""

    db_path: Path
    media_dir: Path
    expected_counts: dict[str, int]
    media_files: dict[str, bytes]  # relative path under media_dir -> content

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


def _migrate(database_url: str) -> None:
    """Run the real migration chain (repo-root alembic.ini) onto the URL."""
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _seed(database_url: str) -> dict[str, int]:
    """Insert representative rows; returns the per-table expected row counts."""
    now = datetime.now(UTC)
    engine = create_db_engine(database_url)
    try:
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        with session_scope(factory) as session:
            goal = WeeklyGoal(week_start=date(2026, 7, 6), title="Ship the hardening phase")
            session.add(goal)
            session.flush()  # goal.id for the FK below
            session.add_all(
                [
                    Task(
                        title="Write the restore drill",
                        goal_id=goal.id,
                        est_minutes=90,
                        deadline=now + timedelta(days=2),
                    ),
                    Task(title="Flood the trigger engine", est_minutes=45),
                    FoodLog(
                        logged_at=now,
                        description="Bibimbap with extra vegetables",
                        media_path="food/lunch.jpg",
                        meal_type="lunch",
                        source="telegram",
                    ),
                    DecisionRecord(
                        kind=DecisionKind.ALERT,
                        tree={"id": "root", "type": "rule", "label": "stress_spike"},
                        summary="Proposed moving the 14:00 focus block",
                    ),
                    TriggerEvent(
                        fired_at=now,
                        rule_id="stress_spike_vs_baseline",
                        dedup_key="stress_spike_vs_baseline:2026-07-09",
                        alert_sent=True,
                        payload={"summary": "Stress spike vs baseline"},
                    ),
                    TriggerEvent(
                        fired_at=now,
                        rule_id="deadline_risk",
                        dedup_key="deadline_risk:abc123",
                        alert_sent=False,
                        payload={"push": {"suppressed_reason": "daily_budget"}},
                    ),
                    CalendarEventMirror(
                        external_id="evt-1",
                        calendar_source=CalendarSource.GOOGLE,
                        summary="Standup",
                        start_at=now,
                        end_at=now + timedelta(minutes=30),
                    ),
                ]
            )
    finally:
        engine.dispose()
    return {
        "weekly_goal": 1,
        "task": 2,
        "food_log": 1,
        "decision_record": 1,
        "trigger_event": 2,
        "calendar_event_mirror": 1,
    }


@pytest.fixture
def seeded_store(tmp_path: Path) -> SeededStore:
    """Migrated + seeded file-backed store with media files on disk."""
    live = tmp_path / "live"
    db_path = live / "healthmes.db"
    media_dir = live / "media"
    database_url = f"sqlite:///{db_path}"

    _migrate(database_url)  # create_db_engine makes the parent dir on demand
    expected_counts = _seed(database_url)

    media_files = {
        "food/lunch.jpg": b"\xff\xd8\xff\xe0 fake jpeg bytes",
        "medical/voice-note.m4a": b"fake m4a bytes " * 8,
    }
    for relative, content in media_files.items():
        target = media_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    return SeededStore(
        db_path=db_path,
        media_dir=media_dir,
        expected_counts=expected_counts,
        media_files=media_files,
    )
