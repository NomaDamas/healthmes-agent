"""LocalDirectoryProvider + weekly job factory tests."""

from datetime import UTC, datetime

import pytest

from healthmes.backup.local import LocalDirectoryProvider, build_backup_job
from healthmes.backup.provider import BackupError, BackupProvider, SnapshotInfo
from healthmes.backup.snapshot import (
    DataLocations,
    resolve_backup_dir,
    resolve_data_locations,
    resolve_passphrase,
)
from healthmes.config import Settings

T1 = datetime(2026, 7, 5, 3, 30, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 12, 3, 30, 0, tzinfo=UTC)


def make_provider(source_env, backup_dir, clock=None, passphrase=...):
    return LocalDirectoryProvider(
        backup_dir,
        locations=source_env.locations,
        passphrase=source_env.passphrase if passphrase is ... else passphrase,
        clock=clock,
    )


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite:///{tmp_path / 'data' / 'healthmes.db'}",
        "data_dir": tmp_path / "data",
        "scheduler_enabled": False,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


class TestProvider:
    def test_satisfies_backup_provider_protocol(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups")
        assert isinstance(provider, BackupProvider)

    def test_export_snapshot_names_and_info(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        info = provider.export_snapshot()
        assert isinstance(info, SnapshotInfo)
        assert info.name == "healthmes-backup-20260705T033000Z.tar.gz.age"
        assert info.path == tmp_path / "backups" / info.name
        assert info.created_at == T1
        assert info.path.is_file()
        assert info.size_bytes == info.path.stat().st_size > 0

    def test_same_second_exports_get_distinct_names(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        first = provider.export_snapshot()
        second = provider.export_snapshot()
        assert first.path != second.path
        assert second.name == "healthmes-backup-20260705T033000Z-2.tar.gz.age"

    def test_list_snapshots_newest_first_ignoring_strays(self, source_env, tmp_path):
        backup_dir = tmp_path / "backups"
        clock_values = [T1, T2]
        provider = make_provider(
            source_env, backup_dir, clock=lambda: clock_values.pop(0)
        )
        old = provider.export_snapshot()
        new = provider.export_snapshot()
        (backup_dir / "notes.txt").write_text("not a snapshot")
        (backup_dir / "other-backup-20260101T000000Z.tar.gz.age").write_bytes(b"stray")

        listed = provider.list_snapshots()
        assert [info.name for info in listed] == [new.name, old.name]
        assert listed[0].created_at == T2
        assert listed[1].created_at == T1

    def test_list_snapshots_empty_when_dir_missing(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "never-created")
        assert provider.list_snapshots() == []

    def test_restore_by_bare_name(self, source_env, fresh_locations, tmp_path, sqlite_dump):
        exporter = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        info = exporter.export_snapshot()

        target, target_root = fresh_locations()
        restorer = LocalDirectoryProvider(
            tmp_path / "backups", locations=target, passphrase=source_env.passphrase
        )
        restorer.restore(info.name)
        assert sqlite_dump(target_root / "data" / "healthmes.db") == sqlite_dump(
            source_env.db_path
        )

    def test_restore_unknown_name_fails(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups")
        with pytest.raises(BackupError, match="snapshot not found"):
            provider.restore("healthmes-backup-19700101T000000Z.tar.gz.age")

    def test_export_without_passphrase_fails_cleanly(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups", passphrase=None)
        with pytest.raises(BackupError, match="HEALTHMES_BACKUP_PASSPHRASE"):
            provider.export_snapshot()


class TestSettingsResolution:
    def test_backup_dir_defaults_under_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HEALTHMES_BACKUP_DIR", raising=False)
        settings = make_settings(tmp_path)
        assert resolve_backup_dir(settings) == tmp_path / "data" / "backups"

    def test_backup_dir_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "vault"))
        settings = make_settings(tmp_path)
        assert resolve_backup_dir(settings) == tmp_path / "vault"

    def test_passphrase_env_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE", raising=False)
        settings = make_settings(tmp_path)
        assert resolve_passphrase(settings) is None
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", "s3cret")
        assert resolve_passphrase(settings) == "s3cret"

    def test_data_locations_resolution(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        settings = make_settings(tmp_path)
        locations = resolve_data_locations(settings)
        assert locations.database_url == settings.database_url
        assert locations.media_dir == tmp_path / "data" / "media"
        assert locations.ow_database_url is None
        assert locations.hermes_home is None

        monkeypatch.setenv("HEALTHMES_OW_DATABASE_URL", "postgresql+psycopg://ow@localhost/ow")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        locations = resolve_data_locations(settings)
        assert locations.ow_database_url == "postgresql+psycopg://ow@localhost/ow"
        assert locations.hermes_home == tmp_path / "hermes"

    def test_from_settings_wires_everything(self, source_env, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "vault"))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HERMES_HOME", str(source_env.hermes_home))
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
        settings = Settings(
            database_url=source_env.database_url,
            data_dir=source_env.data_dir,
            scheduler_enabled=False,
            _env_file=None,
        )
        provider = LocalDirectoryProvider.from_settings(settings)
        info = provider.export_snapshot()
        assert info.path.parent == tmp_path / "vault"


class TestWeeklyJob:
    def test_skips_with_warning_when_no_passphrase(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE", raising=False)
        settings = make_settings(tmp_path)
        job = build_backup_job(settings)
        with caplog.at_level("WARNING", logger="healthmes.backup.local"):
            job()  # must not raise
        assert "no passphrase configured" in caplog.text

    def test_writes_snapshot_when_configured(self, source_env, tmp_path, monkeypatch):
        backup_dir = tmp_path / "weekly"
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(backup_dir))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HERMES_HOME", str(source_env.hermes_home))
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
        settings = Settings(
            database_url=source_env.database_url,
            data_dir=source_env.data_dir,
            scheduler_enabled=False,
            _env_file=None,
        )
        build_backup_job(settings)()
        snapshots = list(backup_dir.glob("healthmes-backup-*.tar.gz.age"))
        assert len(snapshots) == 1

    def test_logs_and_swallows_failures(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", "pp")
        settings = make_settings(tmp_path, database_url="sqlite:///:memory:")
        job = build_backup_job(settings)
        with caplog.at_level("ERROR", logger="healthmes.backup.local"):
            job()  # in-memory sqlite cannot be dumped; job must swallow it
        assert "Weekly backup failed" in caplog.text

    def test_registers_on_scheduler_hook(self, tmp_path):
        """The callable plugs into the hook left by the triggers scope."""
        from healthmes.engine.scheduler import (
            BACKUP_JOB_ID,
            create_scheduler,
            register_backup_job,
        )

        settings = make_settings(tmp_path)
        scheduler = create_scheduler(settings)
        try:
            job = register_backup_job(scheduler, build_backup_job(settings))
            assert job.id == BACKUP_JOB_ID
            assert any(j.id == BACKUP_JOB_ID for j in scheduler.get_jobs())
        finally:
            if scheduler.running:  # pragma: no cover — never started here
                scheduler.shutdown(wait=False)


class TestRoundTripThroughProvider:
    def test_full_cycle(self, source_env, fresh_locations, tmp_path, tree_snapshot, sqlite_dump):
        """create -> list -> restore through the provider surface only."""
        backup_dir = tmp_path / "cycle"
        exporter = make_provider(source_env, backup_dir, clock=lambda: T1)
        exported = exporter.export_snapshot()

        target, target_root = fresh_locations("cycle-target")
        restorer = LocalDirectoryProvider(
            backup_dir, locations=target, passphrase=source_env.passphrase
        )
        listed = restorer.list_snapshots()
        assert [info.name for info in listed] == [exported.name]

        restorer.restore(listed[0].path)
        assert sqlite_dump(target_root / "data" / "healthmes.db") == sqlite_dump(
            source_env.db_path
        )
        assert tree_snapshot(target.media_dir) == tree_snapshot(source_env.media_dir)


def test_data_locations_is_frozen():
    locations = DataLocations(database_url="sqlite:///x.db")
    with pytest.raises(AttributeError):
        locations.database_url = "other"  # type: ignore[misc]
