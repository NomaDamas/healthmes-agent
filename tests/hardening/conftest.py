"""Fixtures for the hardening suite (docs/PLAN.md Phase 3 + section 11).

``seeded_store`` builds a real, file-backed healthmes store the way
production does — alembic ``upgrade head`` (not ``create_all``) onto sqlite —
and seeds representative rows across the domain tables. The restore drill
snapshots/destroys/restores it; a future backup-provider drill can reuse the
same fixture as its source store. No network, Docker, or credentials.
"""

import hashlib
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
    RawIngestEvent,
    StorageObject,
    Task,
    TriggerEvent,
    WeeklyGoal,
    WellnessEvent,
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
    raw_ingest_dir: Path
    expected_counts: dict[str, int]
    media_files: dict[str, bytes]  # relative path under media_dir -> content
    raw_ingest_files: dict[str, bytes]  # relative path under raw_ingest_dir -> content

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


def _migrate(database_url: str) -> None:
    """Run the real migration chain (repo-root alembic.ini) onto the URL."""
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _seed(
    database_url: str,
    *,
    media_files: dict[str, bytes],
    raw_ingest_files: dict[str, bytes],
) -> dict[str, int]:
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
                        media_path="media/food/lunch.jpg",
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
            for relative, content in media_files.items():
                session.add(
                    StorageObject(
                        data_class="media",
                        relative_path=f"media/{relative}",
                        content_type="application/octet-stream",
                        size_bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        retention_basis_at=now,
                        safe_to_purge=True,
                    )
                )
            for index, (relative, content) in enumerate(
                raw_ingest_files.items()
            ):
                digest = hashlib.sha256(content).hexdigest()
                relative_path = f"raw_ingest/{relative}"
                raw = RawIngestEvent(
                    received_at=now + timedelta(seconds=index),
                    source=f"restore-drill-{index}",
                    content_type="application/octet-stream",
                    path=relative_path,
                    size_bytes=len(content),
                    sha256=digest,
                    parse_status="stored_unparsed",
                    forward_status="not_applicable",
                    records_forwarded=0,
                )
                obj = StorageObject(
                    data_class="raw_payload",
                    relative_path=relative_path,
                    content_type="application/octet-stream",
                    size_bytes=len(content),
                    sha256=digest,
                    retention_basis_at=now,
                    safe_to_purge=True,
                )
                session.add_all((raw, obj))
                session.flush()
                session.add(
                    WellnessEvent(
                        event_type="raw_ingest",
                        observed_at=raw.received_at,
                        recorded_at=raw.received_at,
                        source_provider=raw.source,
                        source_record_id=str(raw.id),
                        capture_method="import",
                        payload={
                            "content_type": raw.content_type,
                            "size_bytes": raw.size_bytes,
                            "parse_status": raw.parse_status,
                            "forward_status": raw.forward_status,
                        },
                        raw_object_id=obj.id,
                    )
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
        "raw_ingest_event": 2,
        "storage_object": 4,
        "wellness_event": 2,
    }


@pytest.fixture
def seeded_store(tmp_path: Path) -> SeededStore:
    """Migrated + seeded file-backed store with media files on disk."""
    live = tmp_path / "live"
    db_path = live / "healthmes.db"
    media_dir = live / "media"
    raw_ingest_dir = live / "raw_ingest"
    database_url = f"sqlite:///{db_path}"

    media_files = {
        "food/lunch.jpg": b"\xff\xd8\xff\xe0 fake jpeg bytes",
        "medical/voice-note.m4a": b"fake m4a bytes " * 8,
    }
    for relative, content in media_files.items():
        target = media_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    raw_ingest_files = {
        "healthkit/2026/08/17/batch.json": (
            b'{"source":"healthkit","samples":[{"type":"heart_rate","value":72}]}'
        ),
        "manual/device-export.bin": b"\x00\xffraw-ingest\x10" + bytes(range(64)),
    }
    for relative, content in raw_ingest_files.items():
        target = raw_ingest_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    _migrate(database_url)  # create_db_engine makes the parent dir on demand
    expected_counts = _seed(
        database_url,
        media_files=media_files,
        raw_ingest_files=raw_ingest_files,
    )

    return SeededStore(
        db_path=db_path,
        media_dir=media_dir,
        raw_ingest_dir=raw_ingest_dir,
        expected_counts=expected_counts,
        media_files=media_files,
        raw_ingest_files=raw_ingest_files,
    )
