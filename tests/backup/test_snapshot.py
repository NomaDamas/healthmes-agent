"""Envelope tests: round trip, encryption, manifest inventory, pg tool paths."""

import hashlib
import io
import json
import os
import tarfile
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from pyrage import passphrase as age_passphrase

from healthmes.backup import snapshot as snapshot_mod
from healthmes.backup.provider import (
    BackupError,
    SnapshotIntegrityError,
    WrongPassphraseError,
)
from healthmes.backup.recovery import load_restore_journal, restore_journal_path
from healthmes.backup.snapshot import (
    HERMES_ARCROOT,
    MANIFEST_ARCNAME,
    MEDIA_ARCROOT,
    RECOVERY_SCOPE_PARTIAL_COMPONENT,
    DataLocations,
    create_snapshot,
    find_pg_tool,
    libpq_env,
    libpq_url,
    parse_snapshot_name,
    read_manifest,
    resolve_data_locations,
    restore_snapshot,
    snapshot_name,
)
from healthmes.config import Settings

CREATED_AT = datetime(2026, 7, 9, 3, 30, 0, tzinfo=UTC)


def test_resolve_data_locations_keeps_memory_sqlite_journalless(
    tmp_path: Path,
) -> None:
    locations = resolve_data_locations(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            data_dir=tmp_path / "data",
            _env_file=None,
        )
    )

    assert locations.restore_state_dir is None


def test_resolve_data_locations_assigns_stable_postgres_restore_state(
    tmp_path: Path,
) -> None:
    first = resolve_data_locations(
        Settings(
            database_url=(
                "postgresql+psycopg://healthmes:first@db.test:5432/healthmes"
            ),
            data_dir=tmp_path / "data",
            _env_file=None,
        )
    )
    rotated = resolve_data_locations(
        Settings(
            database_url=(
                "postgresql+psycopg://healthmes:second@db.test:5432/healthmes"
            ),
            data_dir=tmp_path / "data",
            _env_file=None,
        )
    )

    assert first.restore_state_dir == rotated.restore_state_dir
    assert first.restore_state_dir is not None
    assert first.restore_state_dir.is_relative_to(tmp_path / "data")


def test_resolve_data_locations_honors_explicit_restore_state(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "control" / "restore"
    locations = resolve_data_locations(
        Settings(
            database_url=(
                "postgresql+psycopg://healthmes:secret@db.test/healthmes"
            ),
            data_dir=tmp_path / "data",
            restore_state_dir=configured,
            _env_file=None,
        )
    )

    assert locations.restore_state_dir == configured.resolve()


def test_postgres_restore_requires_persistent_state_directory(
    tmp_path: Path,
) -> None:
    locations = DataLocations(
        database_url=(
            "postgresql+psycopg://healthmes:secret@localhost:5432/"
            "healthmes"
        ),
    )

    with pytest.raises(
        BackupError,
        match="restore_state_dir must be configured",
    ):
        restore_snapshot(
            tmp_path / "missing-snapshot.tar.gz.age",
            passphrase="unused",
            locations=locations,
        )

    assert not (tmp_path / ".restore").exists()


def make_snapshot(source_env, out_dir: Path, **overrides) -> Path:
    out_path = out_dir / snapshot_name(CREATED_AT)
    kwargs = {
        "passphrase": source_env.passphrase,
        "out_path": out_path,
        "created_at": CREATED_AT,
    }
    kwargs.update(overrides)
    create_snapshot(source_env.locations, **kwargs)
    return out_path


def decrypt_tar(path: Path, secret: str) -> tarfile.TarFile:
    plaintext = age_passphrase.decrypt(path.read_bytes(), secret)
    return tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_restore_is_exact(
        self, source_env, fresh_locations, tmp_path, tree_snapshot, sqlite_dump
    ):
        original_db = sqlite_dump(source_env.db_path)
        original_media = tree_snapshot(source_env.media_dir)
        original_hermes = tree_snapshot(source_env.hermes_home)

        out_path = make_snapshot(source_env, tmp_path / "backups")
        target, target_root = fresh_locations()
        restore_snapshot(out_path, passphrase=source_env.passphrase, locations=target)

        # The db goes through sqlite3.Connection.backup (consistent against
        # live writers): logically exact, not byte-identical.
        restored_db = sqlite_dump(target_root / "data" / "healthmes.db")
        assert restored_db == original_db

        assert tree_snapshot(target.media_dir) == original_media

        # The out-of-tree skills symlink is (by design) not in the envelope.
        expected_hermes = dict(original_hermes)
        del expected_hermes["skills/healthmes-planner"]
        assert tree_snapshot(target.hermes_home) == expected_hermes
        # The intra-tree symlink and empty dirs survived exactly.
        assert expected_hermes["memory/current.json"] == ("symlink", "state.json")
        assert expected_hermes["cron"] == ("dir",)

    def test_restore_replaces_stale_target_state(self, source_env, fresh_locations, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        target, _root = fresh_locations()
        target.media_dir.mkdir(parents=True)
        (target.media_dir / "stale.bin").write_bytes(b"should disappear")

        restore_snapshot(out_path, passphrase=source_env.passphrase, locations=target)

        assert not (target.media_dir / "stale.bin").exists()
        assert (target.media_dir / "note.txt").exists()

    def test_snapshot_without_optional_sections(
        self, source_env, fresh_locations, tmp_path, sqlite_dump
    ):
        locations = DataLocations(database_url=source_env.database_url)
        out_path = tmp_path / "bare" / snapshot_name(CREATED_AT)
        manifest = create_snapshot(
            locations,
            passphrase=source_env.passphrase,
            out_path=out_path,
            created_at=CREATED_AT,
        )
        assert manifest["contents"]["media"] is None
        assert manifest["contents"]["hermes_home"] is None
        assert manifest["contents"]["open_wearables_db"] is None

        target, target_root = fresh_locations("bare-target")
        restore_snapshot(out_path, passphrase=source_env.passphrase, locations=target)
        assert sqlite_dump(target_root / "data" / "healthmes.db") == sqlite_dump(source_env.db_path)
        assert not target.media_dir.exists()
        assert not target.hermes_home.exists()

    def test_restore_cannot_split_snapshot_external_component_generation(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        """A restore cannot land between the DB, Open Wearables, and Hermes."""
        sequence: list[str] = []
        ow_stage_entered = Event()
        release_ow_stage = Event()
        restore_attempted = Event()
        restore_admitted = Event()
        failures: list[BaseException] = []
        locations = DataLocations(
            database_url=source_env.database_url,
            ow_database_url=(
                "postgresql+psycopg://ow@localhost/open_wearables"
            ),
            hermes_home=source_env.hermes_home,
        )

        def stage_healthmes(_database_url, stage, *, budget):
            destination = stage / snapshot_mod.HEALTHMES_SQLITE_ARCNAME
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"healthmes-before-restore")
            sequence.append("healthmes")
            return {
                "kind": "sqlite_file",
                "arcname": snapshot_mod.HEALTHMES_SQLITE_ARCNAME,
            }

        def stage_open_wearables(_database_url, stage, *, budget):
            sequence.append("open-wearables-start")
            ow_stage_entered.set()
            assert release_ow_stage.wait(timeout=5)
            destination = stage / snapshot_mod.OW_PG_DUMP_ARCNAME
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"wearables-before-restore")
            sequence.append("open-wearables-finish")
            return {
                "kind": "pg_dump",
                "arcname": snapshot_mod.OW_PG_DUMP_ARCNAME,
            }

        def stage_tree(source, stage, arcroot, *, limits, budget):
            assert source == source_env.hermes_home
            destination = stage / arcroot / "state.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("hermes-before-restore", encoding="utf-8")
            sequence.append("hermes")
            return {
                "arcroot": arcroot,
                "file_count": 1,
                "total_bytes": destination.stat().st_size,
                "skipped": [],
            }

        def admitted_restore(*_args, **_kwargs):
            sequence.append("restore-admitted")
            restore_admitted.set()
            return SimpleNamespace()

        monkeypatch.setattr(
            snapshot_mod,
            "_preflight_snapshot_sources",
            lambda _locations: None,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "global_write_plane_guard",
            lambda _database_url: nullcontext(),
        )
        monkeypatch.setattr(
            snapshot_mod,
            "restore_admission_guard",
            lambda _locations: nullcontext(),
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_stage_healthmes_db",
            stage_healthmes,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_stage_ow_db",
            stage_open_wearables,
        )
        monkeypatch.setattr(snapshot_mod, "_stage_tree", stage_tree)
        monkeypatch.setattr(
            snapshot_mod,
            "_restore_snapshot_admitted",
            admitted_restore,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_validate_restore_payload_before_admission",
            lambda *_args, **_kwargs: b"validated",
        )

        def run_snapshot():
            try:
                create_snapshot(
                    locations,
                    passphrase="pp",
                    out_path=tmp_path / "snapshot.age",
                    created_at=CREATED_AT,
                )
            except BaseException as exc:
                failures.append(exc)

        def run_restore():
            restore_attempted.set()
            try:
                restore_snapshot(
                    tmp_path / "ignored.age",
                    passphrase="pp",
                    locations=locations,
                    snapshot_handle=io.BytesIO(b"ignored"),
                )
            except BaseException as exc:
                failures.append(exc)

        snapshot_thread = Thread(target=run_snapshot, daemon=True)
        snapshot_thread.start()
        assert ow_stage_entered.wait(timeout=5)

        restore_thread = Thread(target=run_restore, daemon=True)
        restore_thread.start()
        assert restore_attempted.wait(timeout=5)
        assert not restore_admitted.wait(timeout=0.1)

        release_ow_stage.set()
        snapshot_thread.join(timeout=5)
        restore_thread.join(timeout=5)

        assert not snapshot_thread.is_alive()
        assert not restore_thread.is_alive()
        assert failures == []
        assert sequence == [
            "healthmes",
            "open-wearables-start",
            "open-wearables-finish",
            "hermes",
            "restore-admitted",
        ]


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class TestEncryption:
    def test_wrong_passphrase_fails_cleanly_and_touches_nothing(
        self, source_env, fresh_locations, tmp_path
    ):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        target, target_root = fresh_locations()

        with pytest.raises(WrongPassphraseError, match="wrong passphrase or corrupted"):
            restore_snapshot(out_path, passphrase="not-the-passphrase", locations=target)

        assert not (target_root / "data").exists()
        assert not target.hermes_home.exists()

    def test_snapshot_is_not_readable_without_decryption(self, source_env, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        raw = out_path.read_bytes()
        assert raw.startswith(b"age-encryption.org/v1")
        # Not a valid tar/gzip stream in the clear.
        with pytest.raises(tarfile.ReadError):
            tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        # And the plaintext payload never appears in the ciphertext.
        assert b"voice memo transcript" not in raw

    def test_empty_passphrase_is_rejected(self, source_env, tmp_path):
        with pytest.raises(BackupError, match="passphrase"):
            make_snapshot(source_env, tmp_path / "backups", passphrase="")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_inventory_matches_archive_contents_exactly(self, source_env, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        with decrypt_tar(out_path, source_env.passphrase) as tar:
            manifest = json.load(tar.extractfile(MANIFEST_ARCNAME))
            actual_files: dict[str, str] = {}
            actual_symlinks: dict[str, str] = {}
            for member in tar.getmembers():
                if member.name == MANIFEST_ARCNAME or member.isdir():
                    continue
                if member.issym():
                    actual_symlinks[member.name] = member.linkname
                else:
                    payload = tar.extractfile(member).read()
                    actual_files[member.name] = hashlib.sha256(payload).hexdigest()

        declared_files = {
            entry["path"]: entry["sha256"]
            for entry in manifest["inventory"]
            if entry["kind"] == "file"
        }
        declared_symlinks = {
            entry["path"]: entry["target"]
            for entry in manifest["inventory"]
            if entry["kind"] == "symlink"
        }
        assert declared_files == actual_files
        assert declared_symlinks == actual_symlinks
        assert declared_symlinks == {f"{HERMES_ARCROOT}/memory/current.json": "state.json"}

    def test_manifest_metadata_and_counts(self, source_env, tmp_path):
        manifest = create_snapshot(
            source_env.locations,
            passphrase=source_env.passphrase,
            out_path=tmp_path / snapshot_name(CREATED_AT),
            created_at=CREATED_AT,
        )
        assert manifest["schema_version"] == snapshot_mod.SCHEMA_VERSION == 2
        assert manifest["created_at"] == CREATED_AT.isoformat()
        recovery = manifest["recovery"]
        assert recovery["scope"] == RECOVERY_SCOPE_PARTIAL_COMPONENT
        assert recovery["full_node_recovery"] is False
        assert recovery["components"]["healthmes_db"]["status"] == "included"
        assert recovery["components"]["media"]["status"] == "included"
        assert recovery["components"]["raw_ingest"]["status"] == "not_configured"
        assert recovery["components"]["open_wearables_db"] == {
            "status": "not_configured",
            "runtime_configured": False,
            "dump_configured": False,
        }
        assert recovery["components"]["hermes_home"]["status"] == "included"
        assert recovery["operational_warnings"] == []
        contents = manifest["contents"]
        assert contents["healthmes_db"] == {
            "kind": "sqlite_file",
            "arcname": "db/healthmes.sqlite3",
        }
        assert contents["media"]["arcroot"] == MEDIA_ARCROOT
        assert contents["media"]["file_count"] == 2
        assert contents["media"]["skipped"] == []
        assert contents["hermes_home"]["file_count"] == 2  # config.yaml + state.json

    def test_out_of_tree_symlink_recorded_and_excluded(self, source_env, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        with decrypt_tar(out_path, source_env.passphrase) as tar:
            manifest = json.load(tar.extractfile(MANIFEST_ARCNAME))
            names = tar.getnames()
        assert f"{HERMES_ARCROOT}/skills/healthmes-planner" not in names
        skipped = manifest["contents"]["hermes_home"]["skipped"]
        assert skipped == [
            {
                "path": f"{HERMES_ARCROOT}/skills/healthmes-planner",
                "reason": "symlink-target-not-normalized",
                "target": str(source_env.outside_skill),
            }
        ]

    def test_contained_parent_relative_symlink_is_preserved(
        self,
        source_env,
        fresh_locations,
        tmp_path,
    ):
        link = source_env.hermes_home / "memory" / "parent-config.yaml"
        os.symlink("../config.yaml", link)

        out_path = make_snapshot(source_env, tmp_path / "backups")
        manifest = read_manifest(out_path, source_env.passphrase)
        inventory = {entry["path"]: entry for entry in manifest["inventory"]}
        archived_path = f"{HERMES_ARCROOT}/memory/parent-config.yaml"
        assert inventory[archived_path] == {
            "path": archived_path,
            "kind": "symlink",
            "target": "../config.yaml",
        }
        assert all(
            entry["path"] != archived_path
            for entry in manifest["contents"]["hermes_home"]["skipped"]
        )

        target, _ = fresh_locations("parent-relative-symlink")
        restore_snapshot(
            out_path,
            passphrase=source_env.passphrase,
            locations=target,
        )
        restored = target.hermes_home / "memory" / "parent-config.yaml"
        assert restored.is_symlink()
        assert os.readlink(restored) == "../config.yaml"
        assert restored.read_text(encoding="utf-8") == "agents: {}\n"

    def test_contained_dot_relative_symlink_remains_backward_compatible(
        self,
        source_env,
        fresh_locations,
        tmp_path,
    ):
        link = source_env.hermes_home / "memory" / "current.json"
        expected_state = (source_env.hermes_home / "memory" / "state.json").read_text(
            encoding="utf-8"
        )
        link.unlink()
        os.symlink("./state.json", link)

        out_path = make_snapshot(source_env, tmp_path / "backups")
        manifest = read_manifest(out_path, source_env.passphrase)
        archived_path = f"{HERMES_ARCROOT}/memory/current.json"
        inventory = {entry["path"]: entry for entry in manifest["inventory"]}
        assert inventory[archived_path] == {
            "path": archived_path,
            "kind": "symlink",
            "target": "./state.json",
        }

        target, _ = fresh_locations("dot-relative-symlink")
        restore_snapshot(
            out_path,
            passphrase=source_env.passphrase,
            locations=target,
        )
        restored = target.hermes_home / "memory" / "current.json"
        assert restored.is_symlink()
        assert os.readlink(restored) == "./state.json"
        assert restored.read_text(encoding="utf-8") == expected_state

    def test_naive_created_at_rejected(self, source_env, tmp_path):
        with pytest.raises(ValueError, match="timezone-aware"):
            make_snapshot(source_env, tmp_path, created_at=datetime(2026, 7, 9, 3, 30))

    def test_read_manifest_roundtrip(self, source_env, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        manifest = read_manifest(out_path, source_env.passphrase)
        assert manifest["created_at"] == CREATED_AT.isoformat()

    def test_runtime_ow_without_dump_warns_but_writes_valid_partial_snapshot(
        self,
        source_env,
        fresh_locations,
        tmp_path,
        sqlite_dump,
        caplog,
    ):
        locations = DataLocations(
            database_url=source_env.database_url,
            media_dir=source_env.media_dir,
            hermes_home=source_env.hermes_home,
            ow_runtime_configured=True,
        )
        out_path = tmp_path / "partial" / snapshot_name(CREATED_AT)
        with caplog.at_level("WARNING", logger="healthmes.backup.snapshot"):
            manifest = create_snapshot(
                locations,
                passphrase=source_env.passphrase,
                out_path=out_path,
                created_at=CREATED_AT,
            )

        assert out_path.is_file()
        assert "Partial backup" in caplog.text
        assert "HEALTHMES_OW_DATABASE_URL is unset" in caplog.text
        assert manifest["recovery"]["full_node_recovery"] is False
        assert manifest["recovery"]["components"]["open_wearables_db"] == {
            "status": "omitted_missing_dump_url",
            "runtime_configured": True,
            "dump_configured": False,
        }
        assert len(manifest["recovery"]["operational_warnings"]) == 1
        assert read_manifest(out_path, source_env.passphrase) == manifest

        target, target_root = fresh_locations("partial-target")
        restore_snapshot(
            out_path,
            passphrase=source_env.passphrase,
            locations=target,
        )
        assert sqlite_dump(target_root / "data" / "healthmes.db") == sqlite_dump(source_env.db_path)

    def test_newer_schema_version_is_refused(self, source_env, tmp_path, monkeypatch):
        monkeypatch.setattr(snapshot_mod, "SCHEMA_VERSION", 99)
        out_path = make_snapshot(source_env, tmp_path / "backups")
        monkeypatch.undo()
        with pytest.raises(BackupError, match="newer than this tool"):
            read_manifest(out_path, source_env.passphrase)

    def test_tampered_archive_fails_integrity_check(self, source_env, fresh_locations, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        # Re-pack the archive with one file's bytes flipped, same manifest.
        plaintext = age_passphrase.decrypt(out_path.read_bytes(), source_env.passphrase)
        workdir = tmp_path / "tamper"
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
            tar.extractall(workdir, filter="data")
        (workdir / MEDIA_ARCROOT / "note.txt").write_text("tampered", encoding="utf-8")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path in sorted(workdir.rglob("*")):
                tar.add(path, arcname=path.relative_to(workdir).as_posix(), recursive=False)
        out_path.write_bytes(age_passphrase.encrypt(buffer.getvalue(), source_env.passphrase))

        target, target_root = fresh_locations("tamper-target")
        with pytest.raises(
            SnapshotIntegrityError,
            match="archive size contradicts inventory|checksum mismatch",
        ):
            restore_snapshot(out_path, passphrase=source_env.passphrase, locations=target)
        assert not (target_root / "data").exists()


# ---------------------------------------------------------------------------
# Database backends
# ---------------------------------------------------------------------------


PG_DUMP_STUB = """#!/bin/sh
printf 'tool:%s\\n' "${0##*/}" >> "$PG_STUB_LOG"
printf '%s\\n' "$@" >> "$PG_STUB_LOG"
printf 'env:PGPASSWORD=%s\\n' "${PGPASSWORD-}" >> "$PG_STUB_LOG"
output_file=
for arg in "$@"; do
  case "$arg" in
    --file=*) output_file="${arg#--file=}" ;;
  esac
done
if [ -n "$output_file" ]; then
  printf 'FAKE-PG-DUMP' > "$output_file"
else
  printf 'FAKE-PG-DUMP'
fi
"""

PG_RESTORE_STUB = """#!/bin/sh
printf 'tool:%s\\n' "${0##*/}" >> "$PG_STUB_LOG"
printf '%s\\n' "$@" >> "$PG_STUB_LOG"
printf 'env:PGPASSWORD=%s\\n' "${PGPASSWORD-}" >> "$PG_STUB_LOG"
printf 'env:PGSSLPASSWORD=%s\\n' "${PGSSLPASSWORD-}" >> "$PG_STUB_LOG"
printf 'env:PGPASSFILE=%s\\n' "${PGPASSFILE-}" >> "$PG_STUB_LOG"
if [ "${0##*/}" = "psql" ]; then
  database_url=
  command=
  for arg in "$@"; do
    case "$arg" in
      --dbname=*) database_url="${arg#--dbname=}" ;;
      --command=*) command="${arg#--command=}" ;;
    esac
  done
  if [ -z "$command" ]; then
    while IFS= read -r line; do
      printf 'sql:%s\\n' "$line" >> "$PG_STUB_LOG"
      case "$line" in
        *HEALTHMES_POSTGRES_RESTORE_TARGET_PID*)
          printf 'HEALTHMES_POSTGRES_RESTORE_TARGET_PID:4242\\n'
          ;;
        *HEALTHMES_POSTGRES_TARGET_IDENTITY_MISMATCH:*)
          if [ "${PG_STUB_IDENTITY_MISMATCH-}" = "1" ]; then
            printf 'ERROR: %s\\n' "${HEALTHMES_IDENTITY_MISMATCH_MARKER-}" >&2
            exit 3
          fi
          ;;
      esac
    done
    if [ "${PG_STUB_PREFIX_ONLY_ERROR-}" = "1" ]; then
      printf 'ERROR: HEALTHMES_POSTGRES_TARGET_IDENTITY_MISMATCH\\n' >&2
      exit 4
    fi
    exit 0
  fi
  database_path="${database_url%%\\?*}"
  database_name="${database_path##*/}"
  case "$database_name" in
    healthmes) database_oid=16384 ;;
    open_wearables) database_oid=16385 ;;
    *) database_oid=19999 ;;
  esac
  case "$command" in
    *pg_control_system*)
      printf '["7675026451568287782","%s"]\\n' "$database_oid"
      ;;
    *"count(*) FROM pg_stat_activity"*)
      printf '0\\n'
      ;;
  esac
fi
if [ "${0##*/}" = "pg_restore" ]; then
  list_mode=0
  for arg in "$@"; do
    case "$arg" in
      --list) list_mode=1 ;;
    esac
  done
  if [ "$list_mode" = 0 ]; then
    if [ "${PG_STUB_RENDER_LARGE-}" = "1" ]; then
      printf 'SELECT 1111111111111111111111111111111111111111111111111111111111111111;\\n'
    else
      printf 'SELECT 1;\\n'
    fi
    if [ "${PG_STUB_RENDER_FAILURE-}" = "1" ]; then
      exit 7
    fi
  fi
fi
"""


def write_stub(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def pg_stubs(tmp_path, monkeypatch):
    """Put fake PostgreSQL tools on PATH and bypass live advisory locks."""
    stub_dir = tmp_path / "stub-bin"
    write_stub(stub_dir, "pg_dump", PG_DUMP_STUB)
    write_stub(stub_dir, "pg_restore", PG_RESTORE_STUB)
    write_stub(stub_dir, "psql", PG_RESTORE_STUB)
    log = tmp_path / "pg-stub.log"
    monkeypatch.setenv("PATH", str(stub_dir))
    monkeypatch.setenv("PG_STUB_LOG", str(log))
    monkeypatch.setattr(
        snapshot_mod,
        "global_write_plane_guard",
        lambda _database_url: nullcontext(),
    )
    monkeypatch.setattr(
        snapshot_mod,
        "payload_generation_guard",
        lambda _database_url: nullcontext(),
    )
    return log


class TestDatabaseBackends:
    def test_in_memory_sqlite_rejected(self, source_env, tmp_path):
        locations = DataLocations(database_url="sqlite:///:memory:")
        with pytest.raises(BackupError, match="in-memory sqlite"):
            create_snapshot(
                locations,
                passphrase=source_env.passphrase,
                out_path=tmp_path / "x.tar.gz.age",
                created_at=CREATED_AT,
            )

    def test_missing_sqlite_file_rejected(self, tmp_path):
        locations = DataLocations(database_url=f"sqlite:///{tmp_path / 'absent.db'}")
        with pytest.raises(BackupError, match="not found"):
            create_snapshot(
                locations, passphrase="pp", out_path=tmp_path / "x.age", created_at=CREATED_AT
            )

    def test_unsupported_backend_rejected(self, tmp_path):
        locations = DataLocations(database_url="mysql://user@localhost/db")
        with pytest.raises(BackupError, match="unsupported database backend"):
            create_snapshot(
                locations, passphrase="pp", out_path=tmp_path / "x.age", created_at=CREATED_AT
            )

    def test_postgres_dump_and_restore_invocations(self, pg_stubs, tmp_path):
        url = "postgresql+psycopg://hm:secret@localhost:5433/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "restore-state",
        )
        out_path = tmp_path / "pg" / snapshot_name(CREATED_AT)
        manifest = create_snapshot(
            locations, passphrase="pp", out_path=out_path, created_at=CREATED_AT
        )
        assert manifest["contents"]["healthmes_db"] == {
            "kind": "pg_dump",
            "arcname": "db/healthmes.dump",
        }
        dump_args = pg_stubs.read_text().splitlines()
        assert "--format=custom" in dump_args
        assert "--no-owner" in dump_args
        # Password never on argv (process listings); it rides in PGPASSWORD.
        assert "--dbname=postgresql://hm@localhost:5433/healthmes" in dump_args
        assert not any("secret" in arg for arg in dump_args if not arg.startswith("env:"))
        assert "env:PGPASSWORD=secret" in dump_args

        pg_stubs.write_text("")  # reset the log for the restore leg
        restore_snapshot(out_path, passphrase="pp", locations=locations)
        restore_args = pg_stubs.read_text().splitlines()
        assert "--clean" in restore_args
        assert "--if-exists" in restore_args
        assert "--exit-on-error" in restore_args
        assert "--no-psqlrc" in restore_args
        assert "--set=ON_ERROR_STOP=1" in restore_args
        assert "--dbname=postgresql://hm@localhost:5433/healthmes" in restore_args
        assert "env:PGPASSWORD=secret" in restore_args
        assert any(arg.endswith("db/healthmes.dump") for arg in restore_args)
        assert "--tuples-only" in restore_args
        assert "--no-align" in restore_args
        assert any("pg_control_system" in arg for arg in restore_args)
        assert any(
            "HEALTHMES_POSTGRES_TARGET_IDENTITY_MISMATCH" in arg
            for arg in restore_args
        )
        assert "--file=-" in restore_args
        assert "sql:BEGIN;" in restore_args
        assert any("DROP SCHEMA %I CASCADE" in arg for arg in restore_args)
        assert "sql:SELECT 1;" in restore_args
        assert "sql:COMMIT;" in restore_args
        assert any("ALLOW_CONNECTIONS false" in arg for arg in restore_args)
        assert any("pg_terminate_backend" in arg for arg in restore_args)
        assert any("count(*) FROM pg_stat_activity" in arg for arg in restore_args)
        assert any("ALLOW_CONNECTIONS true" in arg for arg in restore_args)
        assert [arg for arg in restore_args if arg.startswith("tool:")] == [
            "tool:pg_restore",
            "tool:psql",
            "tool:psql",
            "tool:psql",
            "tool:pg_restore",
            "tool:psql",
            "tool:psql",
            "tool:psql",
            "tool:psql",
            "tool:psql",
        ]

    def test_postgres_success_has_no_unrecoverable_intermediate_journal(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@localhost:5433/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "restore-state",
        )
        out_path = tmp_path / "pg-journal" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")
        real_write = snapshot_mod.write_restore_journal
        observed: list[tuple[str, str | None, tuple[str, ...]]] = []

        def tracked_write(path, journal):
            observed.append(
                (
                    journal.phase,
                    journal.current_postgres,
                    tuple(target.state for target in journal.postgres_targets),
                )
            )
            real_write(path, journal)

        monkeypatch.setattr(
            snapshot_mod,
            "write_restore_journal",
            tracked_write,
        )

        restore_snapshot(out_path, passphrase="pp", locations=locations)

        assert (
            "postgres_in_progress",
            None,
            ("committed",),
        ) not in observed
        assert ("committed", None, ("committed",)) in observed

    def test_ow_dump_included_and_restored(self, pg_stubs, source_env, fresh_locations, tmp_path):
        ow_url = "postgresql+psycopg://ow:pw@localhost:5433/open_wearables"
        locations = DataLocations(
            database_url=source_env.database_url,
            ow_database_url=ow_url,
            media_dir=source_env.media_dir,
            hermes_home=source_env.hermes_home,
            ow_runtime_configured=True,
        )
        out_path = tmp_path / "mixed" / snapshot_name(CREATED_AT)
        manifest = create_snapshot(
            locations, passphrase="pp", out_path=out_path, created_at=CREATED_AT
        )
        assert manifest["contents"]["open_wearables_db"]["arcname"] == "db/open_wearables.dump"
        assert manifest["recovery"]["components"]["open_wearables_db"] == {
            "status": "included",
            "runtime_configured": True,
            "dump_configured": True,
        }
        assert manifest["recovery"]["operational_warnings"] == []

        pg_stubs.write_text("")
        target, _root = fresh_locations("ow-target")
        target_with_ow = DataLocations(
            database_url=target.database_url,
            ow_database_url=ow_url,
            media_dir=target.media_dir,
            hermes_home=target.hermes_home,
        )
        result = restore_snapshot(
            out_path,
            passphrase="pp",
            locations=target_with_ow,
            allow_cross_store_partial=True,
        )
        restore_args = pg_stubs.read_text().splitlines()
        assert "--dbname=postgresql://ow@localhost:5433/open_wearables" in restore_args
        assert "env:PGPASSWORD=pw" in restore_args
        assert result.recovery_mode == "operator_approved_cross_store_partial"
        assert result.recovered_components == (
            "healthmes_db",
            "open_wearables_db",
            "media",
            "hermes_home",
        )

    def test_ow_dump_without_target_fails_before_healthmes_mutation(
        self, pg_stubs, source_env, fresh_locations, tmp_path, sqlite_dump
    ):
        ow_url = "postgresql+psycopg://ow:pw@localhost:5433/open_wearables"
        locations = DataLocations(database_url=source_env.database_url, ow_database_url=ow_url)
        out_path = tmp_path / "owonly" / snapshot_name(CREATED_AT)
        create_snapshot(locations, passphrase="pp", out_path=out_path, created_at=CREATED_AT)

        pg_stubs.write_text("")
        target, target_root = fresh_locations("ow-skip-target")
        target_db = target_root / "data" / "healthmes.db"
        target_db.parent.mkdir(parents=True)
        target_db.write_bytes(source_env.db_path.read_bytes())
        before = sqlite_dump(target_db)
        with pytest.raises(BackupError, match="no restore target is configured"):
            restore_snapshot(out_path, passphrase="pp", locations=target)
        assert sqlite_dump(target_db) == before
        assert pg_stubs.read_text() == ""  # no preflight or restore was invoked

    def test_equivalent_postgres_urls_are_rejected_by_physical_preflight(
        self,
        pg_stubs,
        tmp_path,
    ):
        health_url = "postgresql+psycopg://hm@localhost/healthmes"
        ow_url = "postgresql+psycopg://ow@localhost:5432/healthmes"
        locations = DataLocations(
            database_url=health_url,
            ow_database_url=ow_url,
            restore_state_dir=tmp_path / "restore-state",
        )
        out_path = tmp_path / "overlap" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")

        with pytest.raises(BackupError, match="same physical database"):
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
                allow_cross_store_partial=True,
            )

        restore_log = pg_stubs.read_text()
        assert "--list" in restore_log
        assert restore_log.count("tool:psql") == 2
        assert "--single-transaction" not in restore_log

    def test_physical_postgres_target_overlap_is_rejected_after_live_preflight(
        self,
        pg_stubs,
        tmp_path,
    ):
        health_url = "postgresql+psycopg://hm@db-a.invalid/healthmes"
        ow_url = "postgresql+psycopg://ow@db-b.invalid/healthmes"
        locations = DataLocations(
            database_url=health_url,
            ow_database_url=ow_url,
            restore_state_dir=tmp_path / "restore-state",
        )
        out_path = tmp_path / "physical-overlap" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")

        with pytest.raises(
            BackupError,
            match="same physical database",
        ):
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
                allow_cross_store_partial=True,
            )

        restore_log = pg_stubs.read_text()
        assert restore_log.count("tool:psql") == 2
        assert "--single-transaction" not in restore_log

    def test_postgres_target_identity_is_revalidated_immediately_before_restore(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "restore-state",
        )
        out_path = tmp_path / "identity-drift" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        identities = iter(
            [
                ("original-cluster", 16384),
                ("original-cluster", 16384),
                ("replacement-cluster", 24576),
            ]
        )
        identity_calls: list[str] = []
        restore_started = False
        real_write = snapshot_mod.write_restore_journal
        observed: list[tuple[str, str | None, tuple[str, ...]]] = []

        def changing_identity(database_url):
            identity_calls.append(database_url)
            return next(identities)

        def unexpected_restore(*_args):
            nonlocal restore_started
            restore_started = True

        def tracked_write(path, journal):
            observed.append(
                (
                    journal.phase,
                    journal.current_postgres,
                    tuple(target.state for target in journal.postgres_targets),
                )
            )
            real_write(path, journal)

        monkeypatch.setattr(
            snapshot_mod,
            "_preflight_pg_target",
            changing_identity,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_pg_restore_from",
            unexpected_restore,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "write_restore_journal",
            tracked_write,
        )

        with pytest.raises(
            BackupError,
            match="identity changed since preflight.*immediately before pg_restore",
        ):
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
            )

        assert identity_calls == [url, url, url]
        assert restore_started is False
        assert observed[-1] == ("prepared", None, ("pending",))
        assert not restore_journal_path(locations.restore_state_dir).exists()

    def test_postgres_identity_drift_inside_restore_transaction_is_not_ambiguous(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "restore-state",
        )
        out_path = tmp_path / "same-session-identity-drift" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")
        monkeypatch.setenv("PG_STUB_IDENTITY_MISMATCH", "1")

        with pytest.raises(
            BackupError,
            match="identity changed inside the restore transaction",
        ) as excinfo:
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
            )

        assert "commit outcome unknown" not in str(excinfo.value)
        assert [line for line in pg_stubs.read_text().splitlines() if line.startswith("tool:")] == [
            "tool:pg_restore",
            "tool:psql",
            "tool:psql",
            "tool:psql",
            "tool:pg_restore",
            "tool:psql",
            "tool:psql",
            "tool:psql",
            "tool:psql",
            "tool:psql",
        ]
        restore_sql = [
            line
            for line in pg_stubs.read_text().splitlines()
            if line.startswith("sql:")
        ]
        assert "sql:BEGIN;" in restore_sql
        assert any(
            "HEALTHMES_POSTGRES_TARGET_IDENTITY_MISMATCH:" in line
            for line in restore_sql
        )
        assert not any("DROP SCHEMA %I CASCADE" in line for line in restore_sql)
        assert "sql:SELECT 1;" not in restore_sql
        assert "sql:COMMIT;" not in restore_sql

    def test_pg_restore_preparation_failure_never_starts_psql_restore(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "restore-state",
        )
        out_path = tmp_path / "render-failure" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")
        monkeypatch.setenv("PG_STUB_RENDER_FAILURE", "1")

        with pytest.raises(
            BackupError,
            match="pg_restore failed while preparing restore SQL",
        ):
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
            )

        tools = [
            line
            for line in pg_stubs.read_text().splitlines()
            if line.startswith("tool:")
        ]
        assert tools == [
            "tool:pg_restore",
            "tool:psql",
            "tool:psql",
            "tool:psql",
            "tool:pg_restore",
        ]

    def test_pg_restore_nested_expansion_is_bounded_before_psql_starts(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        dump_path = tmp_path / "nested-expansion.dump"
        dump_path.write_bytes(b"x")
        monkeypatch.setenv("PG_STUB_RENDER_LARGE", "1")
        limits = snapshot_mod.SnapshotResourceLimits(
            max_encrypted_bytes=1024,
            max_decrypted_bytes=1024,
            max_members=10,
            max_member_bytes=32,
            max_expanded_bytes=32,
            max_compression_ratio=100.0,
            min_free_bytes=0,
        )

        with pytest.raises(
            BackupError,
            match="nested expansion limit",
        ):
            snapshot_mod._pg_restore_from(
                url,
                dump_path,
                ("7675026451568287782", 16384),
                limits=limits,
            )

        assert [
            line for line in pg_stubs.read_text().splitlines() if line.startswith("tool:")
        ] == ["tool:pg_restore"]

    def test_pg_restore_begin_pipe_failure_is_not_commit_unknown(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        dump_path = tmp_path / "begin-write-failure.dump"
        dump_path.write_bytes(b"x")

        def render_restore_sql(
            _pg_restore,
            _dump_path,
            restore_sql,
            *,
            env,
            limits,
        ):
            restore_sql.write("SELECT 1;\n")

        class FailingStdin:
            def __init__(self):
                self.closed = False

            def write(self, payload):
                if payload == "BEGIN;\n":
                    raise BrokenPipeError("injected BEGIN pipe failure")
                return len(payload)

            def flush(self):
                return None

            def close(self):
                self.closed = True

        class FakePsql:
            def __init__(self):
                self.stdin = FailingStdin()
                self.stdout = io.StringIO(
                    f"{snapshot_mod._POSTGRES_TARGET_PID_MARKER}4242\n"
                )
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        fake_psql = FakePsql()
        monkeypatch.setattr(snapshot_mod, "_render_pg_restore_sql", render_restore_sql)
        monkeypatch.setattr(
            snapshot_mod.subprocess,
            "Popen",
            lambda *_args, **_kwargs: fake_psql,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_set_postgres_connections_allowed",
            lambda _database_url, *, allowed, maintenance_url=None: (
                maintenance_url or "postgresql://hm@router.invalid/postgres"
            ),
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_terminate_postgres_target_sessions",
            lambda *_args, **_kwargs: None,
        )

        with pytest.raises(
            snapshot_mod._PostgresRestoreNotStarted,
            match="restore did not start.*BEGIN pipe failure",
        ):
            snapshot_mod._pg_restore_from(
                url,
                dump_path,
                ("7675026451568287782", 16384),
            )

    def test_pg_restore_wait_timeout_reenables_target_connections(
        self,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        dump_path = tmp_path / "wait-timeout.dump"
        dump_path.write_bytes(b"x")
        admission: list[bool] = []

        def render_restore_sql(
            _pg_restore,
            _dump_path,
            restore_sql,
            *,
            env,
            limits,
        ):
            restore_sql.write("SELECT 1;\n")

        class RecordingStdin(io.StringIO):
            def close(self):
                super().close()

        class HungPsql:
            def __init__(self):
                self.stdin = RecordingStdin()
                self.stdout = io.StringIO(
                    f"{snapshot_mod._POSTGRES_TARGET_PID_MARKER}4242\n"
                )
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                if self.returncode is None:
                    raise snapshot_mod.subprocess.TimeoutExpired(
                        "psql",
                        timeout,
                    )
                return self.returncode

        monkeypatch.setattr(
            snapshot_mod,
            "_render_pg_restore_sql",
            render_restore_sql,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_require_pg_tool",
            lambda *_args, **_kwargs: Path("/fake/pg-tool"),
        )
        monkeypatch.setattr(
            snapshot_mod.subprocess,
            "Popen",
            lambda *_args, **_kwargs: HungPsql(),
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_set_postgres_connections_allowed",
            lambda _database_url, *, allowed, maintenance_url=None: (
                admission.append(allowed)
                or maintenance_url
                or "postgresql://hm@router.invalid/postgres"
            ),
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_terminate_postgres_target_sessions",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_POSTGRES_TOOL_TIMEOUT_SECONDS",
            0.02,
        )

        with pytest.raises(
            BackupError,
            match="psql restore timed out",
        ):
            snapshot_mod._pg_restore_from(
                url,
                dump_path,
                ("7675026451568287782", 16384),
            )

        assert admission == [False, True]

    def test_pg_restore_reserves_space_for_nested_expansion(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        dump_path = tmp_path / "nested-capacity.dump"
        dump_path.write_bytes(b"x")
        limits = snapshot_mod.SnapshotResourceLimits(
            max_encrypted_bytes=1024,
            max_decrypted_bytes=1024,
            max_members=10,
            max_member_bytes=32,
            max_expanded_bytes=32,
            max_compression_ratio=100.0,
            min_free_bytes=1,
        )
        monkeypatch.setattr(
            snapshot_mod.shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(free=32),
        )

        with pytest.raises(
            BackupError,
            match="insufficient disk space for PostgreSQL restore SQL expansion",
        ):
            snapshot_mod._pg_restore_from(
                url,
                dump_path,
                ("7675026451568287782", 16384),
                limits=limits,
            )

        assert not pg_stubs.exists() or pg_stubs.read_text() == ""

    def test_postgres_disable_permission_failure_is_not_a_commit_unknown(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "permission-state",
        )
        out_path = tmp_path / "permission" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")
        calls: list[bool] = []

        def connection_fence(_database_url, *, allowed, maintenance_url=None):
            calls.append(allowed)
            if not allowed:
                raise BackupError("permission denied before connection fencing")
            return maintenance_url or "postgresql://hm@router.invalid/postgres"

        monkeypatch.setattr(
            snapshot_mod,
            "_set_postgres_connections_allowed",
            connection_fence,
        )

        with pytest.raises(BackupError) as excinfo:
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
            )

        assert "permission denied before connection fencing" in str(excinfo.value)
        assert "commit outcome unknown" not in str(excinfo.value)
        assert calls == [False, True]
        assert not restore_journal_path(locations.restore_state_dir).exists()

    def test_postgres_session_termination_failure_is_not_a_commit_unknown(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "termination-state",
        )
        out_path = tmp_path / "termination" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")
        calls: list[bool] = []

        def connection_fence(_database_url, *, allowed, maintenance_url=None):
            calls.append(allowed)
            return maintenance_url or "postgresql://hm@router.invalid/postgres"

        monkeypatch.setattr(
            snapshot_mod,
            "_set_postgres_connections_allowed",
            connection_fence,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_terminate_postgres_target_sessions",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                BackupError("could not terminate target sessions")
            ),
        )

        with pytest.raises(BackupError) as excinfo:
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
            )

        assert "could not terminate target sessions" in str(excinfo.value)
        assert "commit outcome unknown" not in str(excinfo.value)
        assert calls == [False, True]
        assert not restore_journal_path(locations.restore_state_dir).exists()

    def test_ambiguous_postgres_fence_ack_requires_manual_recovery_without_commit_claim(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "ambiguous-fence-state",
        )
        out_path = tmp_path / "ambiguous-fence" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")
        calls: list[bool] = []

        def ambiguous_connection_fence(
            _database_url,
            *,
            allowed,
            maintenance_url=None,
        ):
            calls.append(allowed)
            raise BackupError("connection-fence acknowledgement was lost")

        monkeypatch.setattr(
            snapshot_mod,
            "_set_postgres_connections_allowed",
            ambiguous_connection_fence,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_postgres_connections_allowed",
            lambda *_args, **_kwargs: (
                False,
                "postgresql://hm@router.invalid/postgres",
            ),
        )

        with pytest.raises(
            BackupError,
            match="connection admission unknown=.*not_started",
        ) as excinfo:
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
            )

        assert "commit outcome unknown" not in str(excinfo.value)
        assert calls == [False, True]
        journal_path = restore_journal_path(locations.restore_state_dir)
        journal = load_restore_journal(journal_path)
        assert journal is not None
        assert journal.phase == "manual_recovery_required"
        assert [target.state for target in journal.postgres_targets] == [
            "fence_unknown"
        ]
        with pytest.raises(
            BackupError,
            match="connection admission uncertain",
        ):
            snapshot_mod.recover_incomplete_restore(locations)

    def test_restore_error_with_identity_prefix_is_still_commit_ambiguous(
        self,
        pg_stubs,
        tmp_path,
        monkeypatch,
    ):
        url = "postgresql+psycopg://hm@router.invalid/healthmes"
        locations = DataLocations(
            database_url=url,
            restore_state_dir=tmp_path / "restore-state",
        )
        out_path = tmp_path / "identity-prefix-error" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )
        pg_stubs.write_text("")
        monkeypatch.setenv("PG_STUB_PREFIX_ONLY_ERROR", "1")

        with pytest.raises(BackupError) as excinfo:
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=locations,
            )

        message = str(excinfo.value)
        assert "commit outcome unknown=['healthmes_db']" in message
        assert "identity changed inside the restore transaction" not in message
        assert "sql:SELECT 1;" in pg_stubs.read_text().splitlines()

    def test_cross_store_restore_fails_closed_before_live_mutation(
        self,
        pg_stubs,
        source_env,
        fresh_locations,
        tmp_path,
        sqlite_dump,
        monkeypatch,
    ):
        ow_url = "postgresql+psycopg://ow:pw@localhost:5433/open_wearables"
        locations = DataLocations(
            database_url=source_env.database_url,
            ow_database_url=ow_url,
            media_dir=source_env.media_dir,
        )
        out_path = tmp_path / "mixed-fail-closed" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )

        target, target_root = fresh_locations("mixed-fail-closed-target")
        target_db = target_root / "data" / "healthmes.db"
        target_db.parent.mkdir(parents=True)
        target_db.write_bytes(source_env.db_path.read_bytes())
        connection = snapshot_mod.sqlite3.connect(target_db)
        try:
            connection.execute(
                "INSERT INTO task (title, status) VALUES (?, ?)",
                ("live generation", "live"),
            )
            connection.commit()
        finally:
            connection.close()
        target.media_dir.mkdir(parents=True)
        live_media = target.media_dir / "live.bin"
        live_media.write_bytes(b"live media")
        before = sqlite_dump(target_db)
        target = DataLocations(
            database_url=target.database_url,
            ow_database_url=ow_url,
            media_dir=target.media_dir,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_apply_swap",
            lambda _operation: pytest.fail("local mutation must not start"),
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_pg_restore_from",
            lambda *_args: pytest.fail("PostgreSQL mutation must not start"),
        )
        pg_stubs.write_text("")

        with pytest.raises(BackupError, match="distributed atomic commit"):
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=target,
            )

        assert sqlite_dump(target_db) == before
        assert live_media.read_bytes() == b"live media"
        restore_log = pg_stubs.read_text()
        assert restore_log.count("tool:psql") == 0
        assert "--single-transaction" not in restore_log

    def test_postgres_failure_rolls_back_local_database_and_tree(
        self,
        pg_stubs,
        source_env,
        fresh_locations,
        tmp_path,
        sqlite_dump,
        monkeypatch,
    ):
        ow_url = "postgresql+psycopg://ow:pw@localhost:5433/open_wearables"
        locations = DataLocations(
            database_url=source_env.database_url,
            ow_database_url=ow_url,
            media_dir=source_env.media_dir,
        )
        out_path = tmp_path / "mixed-rollback" / snapshot_name(CREATED_AT)
        create_snapshot(
            locations,
            passphrase="pp",
            out_path=out_path,
            created_at=CREATED_AT,
        )

        target, target_root = fresh_locations("mixed-rollback-target")
        target_db = target_root / "data" / "healthmes.db"
        target_db.parent.mkdir(parents=True)
        target_db.write_bytes(source_env.db_path.read_bytes())
        connection = snapshot_mod.sqlite3.connect(target_db)
        try:
            connection.execute(
                "INSERT INTO task (title, status) VALUES (?, ?)",
                ("live generation", "live"),
            )
            connection.commit()
        finally:
            connection.close()
        target.media_dir.mkdir(parents=True)
        live_media = target.media_dir / "live.bin"
        live_media.write_bytes(b"live media")
        before = sqlite_dump(target_db)
        target = DataLocations(
            database_url=target.database_url,
            ow_database_url=ow_url,
            media_dir=target.media_dir,
        )
        monkeypatch.setattr(
            snapshot_mod,
            "_pg_restore_from",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                BackupError("injected PostgreSQL restore failure")
            ),
        )

        with pytest.raises(
            BackupError,
            match="injected PostgreSQL restore failure",
        ) as excinfo:
            restore_snapshot(
                out_path,
                passphrase="pp",
                locations=target,
                allow_cross_store_partial=True,
            )

        assert "commit outcome unknown=['open_wearables_db']" in str(excinfo.value)
        assert "inspect every listed PostgreSQL target before retrying" in str(excinfo.value)
        assert sqlite_dump(target_db) == before
        assert live_media.read_bytes() == b"live media"
        assert not (target.media_dir / "note.txt").exists()

    def test_snapshot_kind_must_match_target_backend(self, source_env, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        postgres_target = DataLocations(
            database_url="postgresql+psycopg://u@localhost/db",
            restore_state_dir=tmp_path / "restore-state",
        )
        with pytest.raises(BackupError, match="sqlite database but the target"):
            restore_snapshot(out_path, passphrase=source_env.passphrase, locations=postgres_target)

    def test_pg_dump_missing_gives_actionable_error(self, tmp_path, monkeypatch):
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        locations = DataLocations(database_url="postgresql+psycopg://u@localhost/db")
        with pytest.raises(BackupError, match="brew install postgresql@16"):
            create_snapshot(
                locations, passphrase="pp", out_path=tmp_path / "x.age", created_at=CREATED_AT
            )


# ---------------------------------------------------------------------------
# Tool discovery + naming helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_find_pg_tool_prefers_path(self, tmp_path, monkeypatch):
        stub = write_stub(tmp_path / "bin", "pg_dump", "#!/bin/sh\n")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        assert find_pg_tool("pg_dump") == stub

    def test_find_pg_tool_brew_prefix_fallback(self, tmp_path, monkeypatch):
        keg = tmp_path / "keg"
        write_stub(keg / "bin", "pg_dump", "#!/bin/sh\n")
        brew_dir = tmp_path / "brew-bin"
        write_stub(brew_dir, "brew", f'#!/bin/sh\necho "{keg}"\n')
        monkeypatch.setenv("PATH", str(brew_dir))
        assert find_pg_tool("pg_dump") == keg / "bin" / "pg_dump"

    def test_find_pg_tool_absent_everywhere(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        assert find_pg_tool("pg_dump") is None

    def test_libpq_url_strips_driver_and_password(self):
        # The URL travels on pg_dump/pg_restore argv (visible in `ps aux`);
        # credentials must never ride along — the password goes via PGPASSWORD.
        assert (
            libpq_url("postgresql+psycopg://hm:pw@localhost:5432/healthmes")
            == "postgresql://hm@localhost:5432/healthmes"
        )

    def test_libpq_url_strips_query_credentials_but_keeps_transport_options(self):
        rendered = libpq_url(
            "postgresql+psycopg://hm:url-secret@localhost:5432/healthmes"
            "?sslmode=require&sslpassword=tls-secret"
            "&passfile=%2Fprivate%2Fpgpass&password=query-secret"
        )

        assert rendered == ("postgresql://hm@localhost:5432/healthmes?sslmode=require")
        assert "secret" not in rendered
        assert "pgpass" not in rendered

    def test_libpq_env_carries_password_privately(self, monkeypatch):
        monkeypatch.delenv("PGPASSWORD", raising=False)
        monkeypatch.delenv("PGSSLPASSWORD", raising=False)
        monkeypatch.delenv("PGPASSFILE", raising=False)
        env = libpq_env("postgresql+psycopg://hm:s3cr3t@localhost:5432/healthmes")
        assert env["PGPASSWORD"] == "s3cr3t"

        no_password = libpq_env("postgresql://hm@localhost:5432/healthmes")
        assert "PGPASSWORD" not in no_password

        query_credentials = libpq_env(
            "postgresql://hm:url-secret@localhost/healthmes"
            "?password=query-secret&sslpassword=tls-secret"
            "&passfile=%2Fprivate%2Fpgpass"
        )
        assert query_credentials["PGPASSWORD"] == "url-secret"
        assert query_credentials["PGSSLPASSWORD"] == "tls-secret"
        assert query_credentials["PGPASSFILE"] == "/private/pgpass"

    def test_postgres_identity_comes_from_live_cluster_and_database(self, pg_stubs):
        first = snapshot_mod._preflight_pg_target("postgresql://hm@LOCALHOST/healthmes")
        second = snapshot_mod._preflight_pg_target("postgresql://ow@127.0.0.1:5432/healthmes")

        assert first == ("7675026451568287782", 16384)
        assert second == first
        assert pg_stubs.read_text().count("tool:psql") == 2

    def test_snapshot_name_roundtrip_and_utc_normalization(self):
        name = snapshot_name(CREATED_AT)
        assert name == "healthmes-backup-20260709T033000Z.tar.gz.age"
        assert parse_snapshot_name(name) == CREATED_AT
        assert parse_snapshot_name("healthmes-backup-20260709T033000Z-2.tar.gz.age") == CREATED_AT
        assert parse_snapshot_name("random-file.tar.gz.age") is None
        assert parse_snapshot_name("healthmes-backup-garbage.tar.gz.age") is None
