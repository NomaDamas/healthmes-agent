"""Restore-drill smoke (docs/PLAN.md §9 + §10 Phase 3: 복원 훈련).

Drives the real backup seam end-to-end on a live, migrated, seeded store
(the ``seeded_store`` fixture): ``LocalDirectoryProvider.export_snapshot()``
-> destroy the live store -> ``restore()`` -> reopen through the production
engine machinery -> count rows, check the alembic stamp, verify media bytes
and raw-ingest bytes, and confirm that the store accepts new writes. Also
drills the §9 encryption promise: a wrong passphrase must fail loudly
*before* touching live data.

Everything is offline: sqlite database, local directory provider, pyrage
age encryption in-process. No pg_dump path is exercised here (needs a live
postgres); the sqlite path is the mac-native default and the CI path.
"""

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy.orm import sessionmaker

from healthmes.backup import (
    BackupProvider,
    DataLocations,
    LocalDirectoryProvider,
    WrongPassphraseError,
)
from healthmes.store import (
    Base,
    FoodLog,
    RawIngestEvent,
    RetentionPolicy,
    StorageObject,
    Task,
    TriggerEvent,
    WellnessEvent,
    create_db_engine,
    session_scope,
)

# Test modules cannot import from conftest under --import-mode=importlib;
# the seeded_store fixture (a SeededStore dataclass) comes from conftest.py.
REPO_ROOT = Path(__file__).resolve().parents[2]

PASSPHRASE = "drill-passphrase"


def make_provider(seeded_store, backup_dir: Path, passphrase: str = PASSPHRASE):
    """LocalDirectoryProvider wired to the seeded store's live locations."""
    return LocalDirectoryProvider(
        backup_dir,
        locations=DataLocations(
            database_url=seeded_store.database_url,
            media_dir=seeded_store.media_dir,
            raw_ingest_dir=seeded_store.raw_ingest_dir,
        ),
        passphrase=passphrase,
    )


def destroy_live_store(seeded_store) -> None:
    """The 'disaster': remove the database, media, and raw-ingest trees."""
    seeded_store.db_path.unlink()
    shutil.rmtree(seeded_store.media_dir)
    shutil.rmtree(seeded_store.raw_ingest_dir)


def count_rows(database_url: str) -> dict[str, int]:
    """Reopen the store the production way and count rows per domain table."""
    engine = create_db_engine(database_url)
    try:
        counts: dict[str, int] = {}
        with engine.connect() as connection:
            for name, table in Base.metadata.tables.items():
                counts[name] = connection.execute(
                    sa.select(sa.func.count()).select_from(table)
                ).scalar_one()
        return counts
    finally:
        engine.dispose()


def alembic_head_revision() -> str:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def test_snapshot_restore_reopen_counts_rows(seeded_store, tmp_path: Path) -> None:
    """create snapshot -> destroy live store -> restore -> reopen -> count rows."""
    provider = make_provider(seeded_store, tmp_path / "backups")
    assert isinstance(provider, BackupProvider)  # PLAN §9 seam conformance

    info = provider.export_snapshot()
    assert info.path.is_file() and info.size_bytes > 0
    assert [snapshot.name for snapshot in provider.list_snapshots()] == [info.name]
    # The §9 promise: the envelope is ciphertext, no plaintext markers leak.
    assert b"manifest.json" not in info.path.read_bytes()

    destroy_live_store(seeded_store)
    assert not seeded_store.db_path.exists()
    assert not seeded_store.media_dir.exists()
    assert not seeded_store.raw_ingest_dir.exists()

    provider.restore(info.path)

    # Reopen through the production engine machinery and verify every seeded
    # table has exactly its expected rows. The wearable-retention migration
    # intentionally owns one policy row even when the fixture seeds no policy.
    counts = count_rows(seeded_store.database_url)
    migration_owned_counts = {"retention_policy": 1}
    for table, expected in seeded_store.expected_counts.items():
        assert counts[table] == expected, f"{table}: {counts[table]} != {expected}"
    for table, count in counts.items():
        if table not in seeded_store.expected_counts:
            expected = migration_owned_counts.get(table, 0)
            assert count == expected, f"{table}: {count} != {expected}"

    # The restored database is a *migrated* store stamped at the current head,
    # so future `alembic upgrade head` runs keep working after a restore.
    engine = create_db_engine(seeded_store.database_url)
    try:
        with engine.connect() as connection:
            stamped = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            wearable_policy = connection.execute(
                sa.select(
                    RetentionPolicy.data_class,
                    RetentionPolicy.retention_days,
                    RetentionPolicy.enabled,
                )
            ).one()
        assert stamped == alembic_head_revision()
        assert wearable_policy._mapping == {
            "data_class": "wearable_normalized",
            "retention_days": 30,
            "enabled": True,
        }
    finally:
        engine.dispose()

    # Media and raw-ingest bytes round-trip exactly.
    for relative, content in seeded_store.media_files.items():
        assert (seeded_store.media_dir / relative).read_bytes() == content
    for relative, content in seeded_store.raw_ingest_files.items():
        assert (seeded_store.raw_ingest_dir / relative).read_bytes() == content

    # Every raw payload reference is restored as one coherent generation:
    # RawIngestEvent -> StorageObject -> WellnessEvent -> byte-identical file.
    engine = create_db_engine(seeded_store.database_url)
    try:
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        with factory() as session:
            raw_rows = list(session.scalars(sa.select(RawIngestEvent)))
            storage_by_path = {
                row.relative_path: row
                for row in session.scalars(sa.select(StorageObject))
            }
            events_by_source = {
                (row.source_provider, row.source_record_id): row
                for row in session.scalars(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.event_type == "raw_ingest"
                    )
                )
            }
            assert len(raw_rows) == len(seeded_store.raw_ingest_files)
            for raw in raw_rows:
                file_path = seeded_store.db_path.parent / raw.path
                payload = file_path.read_bytes()
                assert raw.size_bytes == len(payload)
                assert raw.sha256 == hashlib.sha256(payload).hexdigest()
                storage = storage_by_path[raw.path]
                assert storage.size_bytes == raw.size_bytes
                assert storage.sha256 == raw.sha256
                event = events_by_source[(raw.source, str(raw.id))]
                assert event.raw_object_id == storage.id
                assert event.payload["size_bytes"] == raw.size_bytes

            for relative, content in seeded_store.media_files.items():
                storage = storage_by_path[f"media/{relative}"]
                assert storage.size_bytes == len(content)
                assert storage.sha256 == hashlib.sha256(content).hexdigest()
            food = session.scalars(sa.select(FoodLog)).one()
            assert food.media_path == "media/food/lunch.jpg"
            assert (seeded_store.db_path.parent / food.media_path).is_file()
    finally:
        engine.dispose()


def test_restore_by_bare_snapshot_name(seeded_store, tmp_path: Path) -> None:
    """The drill also works from `healthmes backup list` output (bare names)."""
    provider = make_provider(seeded_store, tmp_path / "backups")
    info = provider.export_snapshot()
    destroy_live_store(seeded_store)

    provider.restore(info.name)  # bare name resolves inside the backup dir

    counts = count_rows(seeded_store.database_url)
    assert counts["task"] == seeded_store.expected_counts["task"]


def test_restored_store_accepts_new_writes(seeded_store, tmp_path: Path) -> None:
    """A restored store must be fully operational, not merely readable."""
    provider = make_provider(seeded_store, tmp_path / "backups")
    info = provider.export_snapshot()
    destroy_live_store(seeded_store)
    provider.restore(info.path)

    engine = create_db_engine(seeded_store.database_url)
    try:
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        with session_scope(factory) as session:
            session.add(
                TriggerEvent(
                    fired_at=datetime.now(UTC),
                    rule_id="post_restore_smoke",
                    dedup_key="post_restore_smoke:1",
                    alert_sent=False,
                    payload={"summary": "written after restore"},
                )
            )
        with factory() as session:
            titles = set(session.scalars(sa.select(Task.title)).all())
            assert "Write the restore drill" in titles  # pre-disaster row
            written = session.scalars(
                sa.select(TriggerEvent).where(TriggerEvent.rule_id == "post_restore_smoke")
            ).one()
            assert written.payload == {"summary": "written after restore"}
    finally:
        engine.dispose()


def test_wrong_passphrase_fails_before_touching_live_data(
    seeded_store, tmp_path: Path
) -> None:
    """§9: no plaintext without the passphrase — and a failed restore is a no-op."""
    backup_dir = tmp_path / "backups"
    make_provider(seeded_store, backup_dir).export_snapshot()

    # Mutate the live store after the snapshot so "restore happened" would be
    # observable as a row-count change.
    engine = create_db_engine(seeded_store.database_url)
    try:
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        with session_scope(factory) as session:
            session.add(Task(title="added after snapshot"))
    finally:
        engine.dispose()
    counts_before = count_rows(seeded_store.database_url)

    attacker = make_provider(seeded_store, backup_dir, passphrase="wrong-passphrase")
    [snapshot] = attacker.list_snapshots()  # listing needs no passphrase
    with pytest.raises(WrongPassphraseError):
        attacker.restore(snapshot.path)

    # Live data is untouched: the post-snapshot row is still there.
    assert count_rows(seeded_store.database_url) == counts_before
    assert counts_before["task"] == seeded_store.expected_counts["task"] + 1
