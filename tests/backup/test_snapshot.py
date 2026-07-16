"""Envelope tests: round trip, encryption, manifest inventory, pg tool paths."""

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pyrage import passphrase as age_passphrase

from healthmes.backup import snapshot as snapshot_mod
from healthmes.backup.provider import (
    BackupError,
    SnapshotIntegrityError,
    WrongPassphraseError,
)
from healthmes.backup.snapshot import (
    HERMES_ARCROOT,
    MANIFEST_ARCNAME,
    MEDIA_ARCROOT,
    DataLocations,
    create_snapshot,
    find_pg_tool,
    libpq_env,
    libpq_url,
    parse_snapshot_name,
    read_manifest,
    restore_snapshot,
    snapshot_name,
)

CREATED_AT = datetime(2026, 7, 9, 3, 30, 0, tzinfo=UTC)


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
        assert sqlite_dump(target_root / "data" / "healthmes.db") == sqlite_dump(
            source_env.db_path
        )
        assert not target.media_dir.exists()
        assert not target.hermes_home.exists()


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
                "reason": "symlink-outside-tree",
                "target": str(source_env.outside_skill),
            }
        ]

    def test_naive_created_at_rejected(self, source_env, tmp_path):
        with pytest.raises(ValueError, match="timezone-aware"):
            make_snapshot(source_env, tmp_path, created_at=datetime(2026, 7, 9, 3, 30))

    def test_read_manifest_roundtrip(self, source_env, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        manifest = read_manifest(out_path, source_env.passphrase)
        assert manifest["created_at"] == CREATED_AT.isoformat()

    def test_newer_schema_version_is_refused(self, source_env, tmp_path, monkeypatch):
        monkeypatch.setattr(snapshot_mod, "SCHEMA_VERSION", 99)
        out_path = make_snapshot(source_env, tmp_path / "backups")
        monkeypatch.undo()
        with pytest.raises(BackupError, match="newer than this tool"):
            read_manifest(out_path, source_env.passphrase)

    def test_tampered_archive_fails_integrity_check(
        self, source_env, fresh_locations, tmp_path
    ):
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
        with pytest.raises(SnapshotIntegrityError, match="checksum mismatch"):
            restore_snapshot(out_path, passphrase=source_env.passphrase, locations=target)
        assert not (target_root / "data").exists()


# ---------------------------------------------------------------------------
# Database backends
# ---------------------------------------------------------------------------


PG_DUMP_STUB = """#!/bin/sh
printf '%s\\n' "$@" >> "$PG_STUB_LOG"
printf 'env:PGPASSWORD=%s\\n' "${PGPASSWORD-}" >> "$PG_STUB_LOG"
for arg in "$@"; do
  case "$arg" in
    --file=*) printf 'FAKE-PG-DUMP' > "${arg#--file=}" ;;
  esac
done
"""

PG_RESTORE_STUB = """#!/bin/sh
printf '%s\\n' "$@" >> "$PG_STUB_LOG"
printf 'env:PGPASSWORD=%s\\n' "${PGPASSWORD-}" >> "$PG_STUB_LOG"
"""


def write_stub(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def pg_stubs(tmp_path, monkeypatch):
    """Put fake pg_dump/pg_restore on PATH; returns the argv log file."""
    stub_dir = tmp_path / "stub-bin"
    write_stub(stub_dir, "pg_dump", PG_DUMP_STUB)
    write_stub(stub_dir, "pg_restore", PG_RESTORE_STUB)
    log = tmp_path / "pg-stub.log"
    monkeypatch.setenv("PATH", str(stub_dir))
    monkeypatch.setenv("PG_STUB_LOG", str(log))
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
        locations = DataLocations(database_url=url)
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
        assert "--dbname=postgresql://hm@localhost:5433/healthmes" in restore_args
        assert "env:PGPASSWORD=secret" in restore_args
        assert restore_args[-2].endswith("db/healthmes.dump")

    def test_ow_dump_included_and_restored(self, pg_stubs, source_env, fresh_locations, tmp_path):
        ow_url = "postgresql+psycopg://ow:pw@localhost:5433/open_wearables"
        locations = DataLocations(
            database_url=source_env.database_url,
            ow_database_url=ow_url,
            media_dir=source_env.media_dir,
            hermes_home=source_env.hermes_home,
        )
        out_path = tmp_path / "mixed" / snapshot_name(CREATED_AT)
        manifest = create_snapshot(
            locations, passphrase="pp", out_path=out_path, created_at=CREATED_AT
        )
        assert manifest["contents"]["open_wearables_db"]["arcname"] == "db/open_wearables.dump"

        pg_stubs.write_text("")
        target, _root = fresh_locations("ow-target")
        target_with_ow = DataLocations(
            database_url=target.database_url,
            ow_database_url=ow_url,
            media_dir=target.media_dir,
            hermes_home=target.hermes_home,
        )
        restore_snapshot(out_path, passphrase="pp", locations=target_with_ow)
        restore_args = pg_stubs.read_text().splitlines()
        assert "--dbname=postgresql://ow@localhost:5433/open_wearables" in restore_args
        assert "env:PGPASSWORD=pw" in restore_args

    def test_ow_dump_skipped_with_warning_when_no_target(
        self, pg_stubs, source_env, fresh_locations, tmp_path, caplog
    ):
        ow_url = "postgresql+psycopg://ow:pw@localhost:5433/open_wearables"
        locations = DataLocations(database_url=source_env.database_url, ow_database_url=ow_url)
        out_path = tmp_path / "owonly" / snapshot_name(CREATED_AT)
        create_snapshot(locations, passphrase="pp", out_path=out_path, created_at=CREATED_AT)

        pg_stubs.write_text("")
        target, _root = fresh_locations("ow-skip-target")
        with caplog.at_level("WARNING", logger="healthmes.backup.snapshot"):
            restore_snapshot(out_path, passphrase="pp", locations=target)
        assert "open-wearables" in caplog.text
        assert pg_stubs.read_text() == ""  # pg_restore never invoked

    def test_snapshot_kind_must_match_target_backend(self, source_env, tmp_path):
        out_path = make_snapshot(source_env, tmp_path / "backups")
        postgres_target = DataLocations(database_url="postgresql+psycopg://u@localhost/db")
        with pytest.raises(BackupError, match="sqlite database but the target"):
            restore_snapshot(
                out_path, passphrase=source_env.passphrase, locations=postgres_target
            )

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

    def test_libpq_env_carries_password_privately(self, monkeypatch):
        monkeypatch.delenv("PGPASSWORD", raising=False)
        env = libpq_env("postgresql+psycopg://hm:s3cr3t@localhost:5432/healthmes")
        assert env["PGPASSWORD"] == "s3cr3t"

        no_password = libpq_env("postgresql://hm@localhost:5432/healthmes")
        assert "PGPASSWORD" not in no_password

    def test_snapshot_name_roundtrip_and_utc_normalization(self):
        name = snapshot_name(CREATED_AT)
        assert name == "healthmes-backup-20260709T033000Z.tar.gz.age"
        assert parse_snapshot_name(name) == CREATED_AT
        assert parse_snapshot_name("healthmes-backup-20260709T033000Z-2.tar.gz.age") == CREATED_AT
        assert parse_snapshot_name("random-file.tar.gz.age") is None
        assert parse_snapshot_name("healthmes-backup-garbage.tar.gz.age") is None
