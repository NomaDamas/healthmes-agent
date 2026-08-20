"""RemoteVaultProvider tests — S3-compatible replication of encrypted envelopes.

All S3 traffic is intercepted in-process by moto's ``mock_aws`` (no network,
no real credentials, no Docker). The vault must only ever hold byte-identical
copies of the age-encrypted ``*.tar.gz.age`` envelopes; the seam-guard tests
pin the refusal of anything else (docs/PLAN.md section 9).
"""

import hashlib
import io
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws
from pydantic import SecretStr
from pyrage import passphrase as age_passphrase

from healthmes.__main__ import main
from healthmes.backup import local as local_mod
from healthmes.backup.local import LocalDirectoryProvider, build_backup_job
from healthmes.backup.provider import BackupError, BackupProvider, SnapshotInfo
from healthmes.backup.remote_vault import (
    DEFAULT_VAULT_PREFIX,
    RemoteVaultProvider,
    VaultConfig,
    merge_snapshot_listings,
    resolve_backup_provider_name,
    resolve_vault_config,
)
from healthmes.config import Settings

BUCKET = "healthmes-test-vault"
PREFIX = "vaults/tester"

T1 = datetime(2026, 7, 5, 3, 30, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 12, 3, 30, 0, tzinfo=UTC)


def make_config(**overrides) -> VaultConfig:
    values = dict(
        bucket=BUCKET,
        access_key_id="testing",
        secret_access_key="testing",
        region="us-east-1",
        prefix=PREFIX,
    )
    values.update(overrides)
    return VaultConfig(**values)


@pytest.fixture
def s3(monkeypatch):
    """In-process fake S3 with the test bucket created."""
    # A developer's shell endpoint override must not shape client behavior.
    for var in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def local_provider(source_env, tmp_path):
    clock_values = [T1, T2]
    return LocalDirectoryProvider(
        tmp_path / "backups",
        locations=source_env.locations,
        passphrase=source_env.passphrase,
        clock=lambda: clock_values.pop(0) if clock_values else T2,
    )


@pytest.fixture
def vault(s3, local_provider):
    return RemoteVaultProvider(make_config(), local=local_provider)


def vault_keys(s3) -> list[str]:
    response = s3.list_objects_v2(Bucket=BUCKET)
    return sorted(obj["Key"] for obj in response.get("Contents", []))


def enable_versioning(s3) -> None:
    s3.put_bucket_versioning(
        Bucket=BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )


# ---------------------------------------------------------------------------
# The seam guard: only encrypted snapshot envelopes may leave the machine
# ---------------------------------------------------------------------------


class TestSeamGuard:
    def test_refuses_non_snapshot_file_name(self, vault, s3, tmp_path):
        stray = tmp_path / "healthmes.db"
        stray.write_bytes(b"SQLite format 3\x00 plaintext health data")
        with pytest.raises(BackupError, match=r"refusing to upload .*PLAN\.md section 9"):
            vault.push(stray)
        assert vault_keys(s3) == []

    def test_refuses_plaintext_renamed_to_snapshot_extension(self, vault, s3, tmp_path):
        disguised = tmp_path / "healthmes-backup-20260101T000000Z.tar.gz.age"
        disguised.write_bytes(b"raw sqlite bytes, definitely not an age envelope")
        with pytest.raises(BackupError, match="not an age-encrypted envelope"):
            vault.push(disguised)
        assert vault_keys(s3) == []

    def test_refusal_happens_before_any_vault_call(self, local_provider, tmp_path):
        """No client, no credentials, no bucket needed to be refused."""
        provider = RemoteVaultProvider(make_config(), local=local_provider)
        stray = tmp_path / "notes.txt"
        stray.write_text("hi", encoding="utf-8")
        with pytest.raises(BackupError, match="refusing to upload"):
            provider.push(stray)
        assert provider._s3 is None  # client was never even constructed

    def test_refuses_plaintext_with_forged_age_magic_prefix(self, vault, s3, tmp_path):
        """A plaintext file forged to *start* with the age v1 magic line — but
        with no recipient stanza and no ``--- <MAC>`` terminator — is refused.

        This is the exfiltration the prefix-only check missed: prepend the magic
        to a raw database and it would have sailed through."""
        forged = tmp_path / "healthmes-backup-20260101T000000Z.tar.gz.age"
        forged.write_bytes(b"age-encryption.org/v1\nSQLite format 3\x00 plaintext health data")
        with pytest.raises(BackupError, match="not an age-encrypted envelope"):
            vault.push(forged)
        assert vault_keys(s3) == []

    def test_refuses_age_ciphertext_that_is_not_a_healthmes_snapshot_before_vault_call(
        self,
        local_provider,
        tmp_path,
    ):
        provider = RemoteVaultProvider(make_config(), local=local_provider)
        forged = tmp_path / "healthmes-backup-20260101T000000Z.tar.gz.age"
        forged.write_bytes(
            age_passphrase.encrypt(
                b"not a HealthMes snapshot",
                local_provider._require_passphrase(),
            )
        )

        with pytest.raises(
            BackupError,
            match="encrypted payload is not a valid HealthMes snapshot",
        ):
            provider.push(forged)

        assert provider._s3 is None

    def test_accepts_a_real_age_encrypted_snapshot(self, vault, s3, local_provider):
        """A genuine pyrage-encrypted snapshot passes the structural guard and
        is uploaded byte-identically."""
        local_info = local_provider.export_snapshot()
        remote_info = vault.push(local_info.path)
        assert remote_info.name == local_info.name
        assert vault_keys(s3) == [f"{PREFIX}/{local_info.name}"]
        obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}/{local_info.name}")
        assert obj["Body"].read() == local_info.path.read_bytes()


# ---------------------------------------------------------------------------
# Push / download / list against the fake vault
# ---------------------------------------------------------------------------


class TestPushDownloadList:
    def test_push_uploads_byte_identical_envelope(self, vault, s3, local_provider):
        local_info = local_provider.export_snapshot()
        remote_info = vault.push(local_info.path)

        assert remote_info.name == local_info.name
        assert remote_info.path == Path(f"{PREFIX}/{local_info.name}")
        assert remote_info.created_at == T1
        assert remote_info.size_bytes == local_info.size_bytes

        obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}/{local_info.name}")
        assert obj["Body"].read() == local_info.path.read_bytes()
        assert "healthmes-sha256" in obj["Metadata"]

    def test_push_accepts_bare_snapshot_name(self, vault, s3, local_provider):
        local_info = local_provider.export_snapshot()
        remote_info = vault.push(local_info.name)
        assert vault_keys(s3) == [f"{PREFIX}/{remote_info.name}"]

    def test_push_hashes_and_uploads_the_same_open_file(
        self,
        local_provider,
    ):
        local_info = local_provider.export_snapshot()
        original = local_info.path.read_bytes()

        class ReplacingClient:
            def __init__(self) -> None:
                self.remote = b""

            def put_object(self, **kwargs):
                local_info.path.unlink()
                local_info.path.write_bytes(b"replacement path contents")
                self.remote = kwargs["Body"].read()
                return {}

            def get_object(self, **_kwargs):
                return {
                    "Body": io.BytesIO(self.remote),
                    "ContentLength": len(self.remote),
                    "Metadata": {},
                    "ETag": "",
                }

        client = ReplacingClient()
        provider = RemoteVaultProvider(make_config(), local=local_provider)
        provider._s3 = client

        with pytest.raises(BackupError, match="changed while it was being uploaded"):
            provider.push(local_info.path)

        assert client.remote == original
        assert local_info.path.read_bytes() == b"replacement path contents"

    def test_push_seals_ciphertext_before_same_inode_is_overwritten(
        self,
        local_provider,
    ):
        local_info = local_provider.export_snapshot()
        original = local_info.path.read_bytes()
        plaintext = b"private wellness plaintext" * 100

        class InPlaceReplacingClient:
            def __init__(self) -> None:
                self.remote = b""

            def put_object(self, **kwargs):
                with local_info.path.open("r+b") as raced:
                    raced.seek(0)
                    raced.write(plaintext)
                    raced.truncate()
                    raced.flush()
                    os.fsync(raced.fileno())
                self.remote = kwargs["Body"].read()
                return {}

            def get_object(self, **_kwargs):
                return {
                    "Body": io.BytesIO(self.remote),
                    "ContentLength": len(self.remote),
                    "Metadata": {},
                    "ETag": "",
                }

        client = InPlaceReplacingClient()
        provider = RemoteVaultProvider(make_config(), local=local_provider)
        provider._s3 = client

        with pytest.raises(
            BackupError,
            match="changed while it was being uploaded",
        ):
            provider.push(local_info.path)

        assert client.remote == original
        assert client.remote != plaintext
        assert local_info.path.read_bytes() == plaintext

    def test_list_snapshots_newest_first_ignoring_strays(self, vault, s3, local_provider):
        first = vault.push(local_provider.export_snapshot().path)
        second = vault.push(local_provider.export_snapshot().path)
        s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/notes.txt", Body=b"stray")
        s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/nested/{first.name}", Body=b"nested")
        s3.put_object(Bucket=BUCKET, Key="outside-prefix.tar.gz.age", Body=b"outside")

        listed = vault.list_snapshots()
        assert [info.name for info in listed] == [second.name, first.name]
        assert listed[0].created_at == T2
        assert all(isinstance(info, SnapshotInfo) for info in listed)
        assert listed[0].size_bytes == second.size_bytes

    def test_download_round_trips_bytes_and_refreshes_existing_local_cache(
        self, vault, local_provider
    ):
        local_info = local_provider.export_snapshot()
        original = local_info.path.read_bytes()
        vault.push(local_info.path)
        local_info.path.unlink()

        downloaded = vault.download(local_info.name)
        assert downloaded == local_info.path
        assert downloaded.read_bytes() == original

        downloaded.write_bytes(b"different same-name local cache")
        assert vault.download(local_info.name) == downloaded
        assert downloaded.read_bytes() == original
        with pytest.raises(BackupError, match="already exists"):
            vault.download(local_info.name, overwrite=False)

    def test_satisfies_backup_provider_protocol(self, vault):
        assert isinstance(vault, BackupProvider)


class TestExportAndRestore:
    def test_export_snapshot_keeps_local_and_replicates(self, vault, s3, local_provider):
        info = vault.export_snapshot()
        # Local-first: the returned descriptor is the local copy...
        assert info.path.is_file()
        assert info.path.parent == local_provider.backup_dir
        # ...and the vault holds the byte-identical replica.
        assert vault_keys(s3) == [f"{PREFIX}/{info.name}"]

    def test_export_snapshot_remote_only_removes_local_copy_from_versioned_vault(
        self,
        s3,
        local_provider,
    ):
        enable_versioning(s3)
        provider = RemoteVaultProvider(make_config(), local=local_provider, keep_local=False)
        info = provider.export_snapshot()
        assert info.path == Path(f"{PREFIX}/{info.name}")  # remote descriptor
        assert list(local_provider.backup_dir.glob("*.tar.gz.age")) == []
        assert vault_keys(s3) == [f"{PREFIX}/{info.name}"]

    def test_remote_only_quarantine_recovers_on_local_provider_restart(
        self,
        s3,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        enable_versioning(s3)
        backup_dir = tmp_path / "remote-only-restart"
        local = LocalDirectoryProvider(
            backup_dir,
            locations=source_env.locations,
            passphrase=source_env.passphrase,
            clock=lambda: T1,
        )
        provider = RemoteVaultProvider(
            make_config(),
            local=local,
            keep_local=False,
        )
        real_unlink = local_mod._unlink_snapshot_quarantine_entry
        failed = False

        def fail_once(quarantine):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("injected quarantine unlink interruption")
            return real_unlink(quarantine)

        monkeypatch.setattr(
            local_mod,
            "_unlink_snapshot_quarantine_entry",
            fail_once,
        )
        with pytest.raises(
            BackupError,
            match="quarantine unlink interruption",
        ):
            provider.export_snapshot()

        remote_keys = vault_keys(s3)
        quarantines = list(
            backup_dir.glob(
                f"{local_mod._SNAPSHOT_QUARANTINE_PREFIX}*"
            )
        )
        assert len(remote_keys) == 1
        assert len(quarantines) == 1
        assert list(backup_dir.glob("*.tar.gz.age")) == []

        monkeypatch.setattr(
            local_mod,
            "_unlink_snapshot_quarantine_entry",
            real_unlink,
        )
        LocalDirectoryProvider(
            backup_dir,
            locations=source_env.locations,
            passphrase=source_env.passphrase,
            clock=lambda: T2,
        )

        assert not quarantines[0].exists()
        assert list(backup_dir.glob("*.tar.gz.age")) == []
        assert vault_keys(s3) == remote_keys

    def test_remote_only_non_versioned_vault_preserves_local_copy(
        self,
        s3,
        local_provider,
    ):
        provider = RemoteVaultProvider(
            make_config(),
            local=local_provider,
            keep_local=False,
        )

        with pytest.raises(
            BackupError,
            match="remote-only mode requires an immutable vault generation",
        ):
            provider.export_snapshot()

        local_snapshots = list(local_provider.backup_dir.glob("*.tar.gz.age"))
        assert len(local_snapshots) == 1
        assert vault_keys(s3) == [f"{PREFIX}/{local_snapshots[0].name}"]

    def test_remote_only_same_second_exports_never_overwrite(
        self,
        s3,
        source_env,
        tmp_path,
    ):
        enable_versioning(s3)
        local = LocalDirectoryProvider(
            tmp_path / "remote-only-backups",
            locations=source_env.locations,
            passphrase=source_env.passphrase,
            clock=lambda: T1,
        )
        provider = RemoteVaultProvider(
            make_config(),
            local=local,
            keep_local=False,
        )

        first = provider.export_snapshot()
        first_key = f"{PREFIX}/{first.name}"
        first_payload = s3.get_object(Bucket=BUCKET, Key=first_key)["Body"].read()
        (source_env.media_dir / "note.txt").write_text(
            "changed before the same-second retry",
            encoding="utf-8",
        )
        second = provider.export_snapshot()

        assert first.name == "healthmes-backup-20260705T033000Z.tar.gz.age"
        assert second.name == "healthmes-backup-20260705T033000Z-2.tar.gz.age"
        assert set(vault_keys(s3)) == {
            f"{PREFIX}/{first.name}",
            f"{PREFIX}/{second.name}",
        }
        assert (
            s3.get_object(Bucket=BUCKET, Key=first_key)["Body"].read()
            == first_payload
        )
        assert list(local.backup_dir.glob("*.tar.gz.age")) == []

    def test_push_is_idempotent_for_identical_snapshot(
        self,
        vault,
        s3,
        local_provider,
    ):
        local_info = local_provider.export_snapshot()

        first = vault.push(local_info.path)
        second = vault.push(local_info.path)

        assert second == first
        assert vault_keys(s3) == [f"{PREFIX}/{local_info.name}"]

    def test_restore_downloads_when_not_local(
        self, vault, source_env, fresh_locations, tmp_path, sqlite_dump
    ):
        exported = vault.export_snapshot()
        shutil.rmtree(vault.local.backup_dir)  # lose every local copy

        target, target_root = fresh_locations("vault-target")
        restorer = RemoteVaultProvider(
            make_config(),
            local=LocalDirectoryProvider(
                tmp_path / "restore-side",
                locations=target,
                passphrase=source_env.passphrase,
            ),
        )
        result = restorer.restore(exported.name)
        assert sqlite_dump(target_root / "data" / "healthmes.db") == sqlite_dump(
            source_env.db_path
        )
        assert result.recovery_mode == "recoverable_local_swaps"
        assert result.recovered_components == (
            "healthmes_db",
            "media",
            "hermes_home",
        )
        # The authoritative envelope was materialized as a verified local cache.
        assert (restorer.local.backup_dir / exported.name).is_file()

    def test_restore_remote_snapshot_replaces_different_valid_local_snapshot(
        self,
        vault,
        source_env,
        local_provider,
    ):
        original_note = (source_env.media_dir / "note.txt").read_bytes()
        remote = vault.export_snapshot()
        remote_bytes = remote.path.read_bytes()

        (source_env.media_dir / "note.txt").write_text(
            "different local snapshot",
            encoding="utf-8",
        )
        different = local_provider.export_snapshot()
        assert different.path.read_bytes() != remote_bytes
        different.path.replace(remote.path)

        vault.restore(remote.name)

        assert remote.path.read_bytes() == remote_bytes
        assert (source_env.media_dir / "note.txt").read_bytes() == original_note

    def test_restore_uses_pinned_download_if_cache_name_is_replaced(
        self,
        vault,
        source_env,
        local_provider,
        monkeypatch,
    ):
        original_note = (source_env.media_dir / "note.txt").read_bytes()
        remote = vault.export_snapshot()
        (source_env.media_dir / "note.txt").write_text(
            "live data changed after snapshot",
            encoding="utf-8",
        )
        raced_cache = b"untrusted replacement cache generation"
        real_restore = local_provider.restore_open_snapshot

        def replace_cache_before_restore(
            path,
            handle,
            *,
            allow_cross_store_partial=False,
        ):
            replacement = path.with_name(f".{path.name}.replacement")
            replacement.write_bytes(raced_cache)
            os.replace(replacement, path)
            return real_restore(
                path,
                handle,
                allow_cross_store_partial=allow_cross_store_partial,
            )

        monkeypatch.setattr(
            local_provider,
            "restore_open_snapshot",
            replace_cache_before_restore,
        )

        vault.restore(remote.name)

        assert remote.path.read_bytes() == raced_cache
        assert (source_env.media_dir / "note.txt").read_bytes() == original_note

    def test_restore_remote_never_falls_back_to_same_name_local_snapshot(
        self,
        vault,
        local_provider,
    ):
        local_only = local_provider.export_snapshot()
        local_bytes = local_only.path.read_bytes()

        with pytest.raises(BackupError, match="snapshot not found in vault"):
            vault.restore(local_only.name)

        assert local_only.path.read_bytes() == local_bytes


# ---------------------------------------------------------------------------
# Merged listing
# ---------------------------------------------------------------------------


def _info(name_stamp: str, created_at: datetime, size: int, *, remote: bool) -> SnapshotInfo:
    name = f"healthmes-backup-{name_stamp}.tar.gz.age"
    path = Path(f"{PREFIX}/{name}") if remote else Path(f"/backups/{name}")
    return SnapshotInfo(name=name, path=path, created_at=created_at, size_bytes=size)


class TestMergedListing:
    def test_merge_labels_origin_and_sorts_newest_first(self):
        both_local = _info("20260705T033000Z", T1, 100, remote=False)
        both_remote = _info("20260705T033000Z", T1, 100, remote=True)
        local_only = _info("20260712T033000Z", T2, 200, remote=False)
        remote_only = _info("20260101T000000Z", datetime(2026, 1, 1, tzinfo=UTC), 50, remote=True)

        merged = merge_snapshot_listings([both_local, local_only], [both_remote, remote_only])
        assert [(entry.name, entry.origin) for entry in merged] == [
            (local_only.name, "local"),
            (both_local.name, "both"),
            (remote_only.name, "remote"),
        ]
        # Local descriptor wins when both sides exist (local-first).
        assert merged[1].info is both_local
        assert not merged[1].size_mismatch

    def test_merge_flags_size_mismatch(self):
        local = _info("20260705T033000Z", T1, 100, remote=False)
        remote = _info("20260705T033000Z", T1, 999, remote=True)
        (entry,) = merge_snapshot_listings([local], [remote])
        assert entry.origin == "both"
        assert entry.size_mismatch


# ---------------------------------------------------------------------------
# CLI: create/push/list/restore with --provider remote
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_vault_env(source_env, tmp_path, monkeypatch, s3):
    """CLI environment with both the fake live data and the fake vault wired."""
    monkeypatch.chdir(tmp_path)  # no repo .env in reach
    monkeypatch.setenv("HEALTHMES_DATABASE_URL", source_env.database_url)
    monkeypatch.setenv("HEALTHMES_DATA_DIR", str(source_env.data_dir))
    monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
    monkeypatch.setenv("HERMES_HOME", str(source_env.hermes_home))
    monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("HEALTHMES_VAULT_BUCKET", BUCKET)
    monkeypatch.setenv("HEALTHMES_VAULT_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("HEALTHMES_VAULT_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("HEALTHMES_VAULT_REGION", "us-east-1")
    monkeypatch.setenv("HEALTHMES_VAULT_PREFIX", PREFIX)
    monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
    return SimpleNamespace(source=source_env, backup_dir=tmp_path / "backups")


def create_snapshot_via_cli(capsys, *extra_args) -> str:
    assert main(["backup", "create", *extra_args]) == 0
    out = capsys.readouterr().out
    assert "snapshot written:" in out
    return out.split("snapshot written:")[1].split("(")[0].strip()


class TestCliRoundTrip:
    def test_create_push_wipe_restore(self, cli_vault_env, s3, capsys, sqlite_dump):
        """The scope's canonical drill: create → push → wipe local → restore."""
        source = cli_vault_env.source
        original_note = (source.media_dir / "note.txt").read_bytes()
        reference_db = sqlite_dump(source.db_path)

        path = create_snapshot_via_cli(capsys)
        name = Path(path).name
        assert main(["backup", "push", name]) == 0
        assert f"pushed: {name} -> s3://{BUCKET}/{PREFIX}/{name}" in capsys.readouterr().out

        shutil.rmtree(cli_vault_env.backup_dir)  # the vault now holds the only copy
        (source.media_dir / "note.txt").write_text("mutated after push", encoding="utf-8")

        assert main(["backup", "list", "--provider", "remote"]) == 0
        listing = capsys.readouterr().out
        assert f"{name}\t" in listing
        assert "\tremote" in listing

        assert main(["backup", "restore", name, "--provider", "remote", "--yes"]) == 0
        out = capsys.readouterr().out
        assert f"downloaded from vault: s3://{BUCKET}/{PREFIX}/{name}" in out
        assert "restored:" in out
        assert (source.media_dir / "note.txt").read_bytes() == original_note
        assert sqlite_dump(source.db_path) == reference_db

    def test_remote_cli_restore_uses_the_pinned_download_generation(
        self,
        cli_vault_env,
        s3,
        capsys,
        monkeypatch,
    ):
        source = cli_vault_env.source
        original_note = (source.media_dir / "note.txt").read_bytes()
        path = Path(create_snapshot_via_cli(capsys, "--provider", "remote"))
        name = path.name
        (source.media_dir / "note.txt").write_text(
            "mutated after remote snapshot",
            encoding="utf-8",
        )
        raced_cache = b"replacement cache generation"
        real_restore = LocalDirectoryProvider.restore_open_snapshot

        def replace_cache_before_restore(
            provider,
            snapshot_path,
            handle,
            *,
            allow_cross_store_partial=False,
        ):
            replacement = snapshot_path.with_name(
                f".{snapshot_path.name}.replacement"
            )
            replacement.write_bytes(raced_cache)
            os.replace(replacement, snapshot_path)
            return real_restore(
                provider,
                snapshot_path,
                handle,
                allow_cross_store_partial=allow_cross_store_partial,
            )

        monkeypatch.setattr(
            LocalDirectoryProvider,
            "restore_open_snapshot",
            replace_cache_before_restore,
        )

        assert (
            main(
                [
                    "backup",
                    "restore",
                    name,
                    "--provider",
                    "remote",
                    "--yes",
                ]
            )
            == 0
        )

        assert path.read_bytes() == raced_cache
        assert (source.media_dir / "note.txt").read_bytes() == original_note

    def test_create_provider_remote_writes_local_then_uploads(
        self, cli_vault_env, s3, capsys
    ):
        path = create_snapshot_via_cli(capsys, "--provider", "remote")
        # capsys was consumed inside the helper; re-run to see both lines.
        assert Path(path).is_file()  # local copy kept by default
        name = Path(path).name
        assert vault_keys(s3) == [f"{PREFIX}/{name}"]

    def test_create_remote_only_removes_local_copy(self, cli_vault_env, s3, capsys):
        enable_versioning(s3)
        assert main(["backup", "create", "--provider", "remote", "--remote-only"]) == 0
        captured = capsys.readouterr()
        assert "uploaded to vault:" in captured.out
        assert "only copy" in captured.err
        assert list(cli_vault_env.backup_dir.glob("*.tar.gz.age")) == []
        assert len(vault_keys(s3)) == 1

    def test_remote_only_without_remote_provider_is_refused(self, cli_vault_env, capsys):
        assert main(["backup", "create", "--remote-only"]) == 1
        assert "--remote-only requires --provider remote" in capsys.readouterr().err

    def test_list_merged_labels_all_origins(self, cli_vault_env, capsys):
        local_only = Path(create_snapshot_via_cli(capsys))  # local only
        both = Path(create_snapshot_via_cli(capsys, "--provider", "remote"))
        capsys.readouterr()
        remote_only = Path(create_snapshot_via_cli(capsys, "--provider", "remote"))
        remote_only.unlink()  # now it exists only in the vault

        assert main(["backup", "list", "--provider", "remote"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        origins = {
            line.split("\t")[0]: line.split("\t")[3] for line in lines
        }
        assert origins[local_only.name] == "local"
        assert origins[both.name] == "both"
        assert origins[remote_only.name] == "remote"

    def test_plain_local_list_output_is_unchanged(self, cli_vault_env, capsys):
        create_snapshot_via_cli(capsys)
        assert main(["backup", "list"]) == 0
        out = capsys.readouterr().out
        # Three tab-separated columns — no origin column on the local view.
        assert out.strip().count("\t") == 2

    def test_restore_dry_run_downloads_and_shows_manifest(self, cli_vault_env, capsys):
        path = create_snapshot_via_cli(capsys, "--provider", "remote")
        name = Path(path).name
        Path(path).unlink()

        assert main(["backup", "restore", name, "--provider", "remote"]) == 2
        captured = capsys.readouterr()
        assert "downloaded from vault:" in captured.out
        assert "healthmes db:       sqlite_file" in captured.out
        assert "re-run with --yes" in captured.err

    def test_restore_download_local_io_failure_is_clean(
        self,
        cli_vault_env,
        capsys,
    ):
        path = Path(create_snapshot_via_cli(capsys, "--provider", "remote"))
        name = path.name
        shutil.rmtree(cli_vault_env.backup_dir)
        cli_vault_env.backup_dir.write_text(
            "not a directory",
            encoding="utf-8",
        )

        assert main(["backup", "restore", name, "--provider", "remote"]) == 1

        captured = capsys.readouterr()
        assert "error: could not write vault snapshot" in captured.err
        assert "Traceback" not in captured.err

    def test_selector_env_var_routes_create_to_vault(self, cli_vault_env, s3, capsys, monkeypatch):
        monkeypatch.setenv("HEALTHMES_BACKUP_PROVIDER", "remote_vault")
        assert main(["backup", "create"]) == 0
        assert "uploaded to vault:" in capsys.readouterr().out
        assert len(vault_keys(s3)) == 1

    def test_provider_flag_overrides_selector(self, cli_vault_env, s3, capsys, monkeypatch):
        monkeypatch.setenv("HEALTHMES_BACKUP_PROVIDER", "remote_vault")
        assert main(["backup", "create", "--provider", "local"]) == 0
        assert "uploaded to vault:" not in capsys.readouterr().out
        assert vault_keys(s3) == []

    def test_invalid_selector_fails_cleanly(self, cli_vault_env, capsys, monkeypatch):
        monkeypatch.setenv("HEALTHMES_BACKUP_PROVIDER", "ftp")
        assert main(["backup", "create"]) == 1
        err = capsys.readouterr().err
        assert "unknown backup provider 'ftp'" in err
        assert "Traceback" not in err

    def test_push_refuses_non_envelope_via_cli(self, cli_vault_env, s3, capsys):
        disguised = cli_vault_env.backup_dir / "healthmes-backup-20260101T000000Z.tar.gz.age"
        disguised.parent.mkdir(parents=True, exist_ok=True)
        disguised.write_bytes(b"plaintext pretending to be a snapshot")
        assert main(["backup", "push", str(disguised)]) == 1
        err = capsys.readouterr().err
        assert "error:" in err and "age-encrypted" in err
        assert vault_keys(s3) == []


# ---------------------------------------------------------------------------
# Error paths stay clean (single-line, actionable, no traceback)
# ---------------------------------------------------------------------------


class _WrongCredentialsClient:
    def put_object(self, **kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "InvalidAccessKeyId",
                    "Message": "The AWS Access Key Id you provided does not exist.",
                }
            },
            "PutObject",
        )


class _CloseFailingBody:
    def __init__(self, payload: bytes, leaked_message: str) -> None:
        self._stream = io.BytesIO(payload)
        self._leaked_message = leaked_message
        self.close_calls = 0

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError(self._leaked_message)


class TestErrorPaths:
    def test_endpoint_connection_error_redacts_all_url_credentials(
        self,
        local_provider,
    ):
        provider = RemoteVaultProvider(
            make_config(
                endpoint_url=(
                    "https://alice:supersecret@example.invalid:9443/"
                    "private/path?token=signed-secret#credential-fragment"
                )
            ),
            local=local_provider,
        )

        with pytest.raises(BackupError) as excinfo:
            with provider._vault_call("list snapshots"):
                raise EndpointConnectionError(
                    endpoint_url=(
                        "https://alice:supersecret@example.invalid:9443/"
                        "private/path?token=signed-secret#credential-fragment"
                    )
                )

        assert str(excinfo.value) == (
            "cannot reach the vault endpoint to list snapshots "
            "(https://example.invalid:9443): check HEALTHMES_VAULT_ENDPOINT"
        )
        rendered = str(excinfo.value)
        for secret in (
            "alice",
            "supersecret",
            "private/path",
            "signed-secret",
            "credential-fragment",
        ):
            assert secret not in rendered
        assert excinfo.value.__cause__ is None

    def test_wrong_credentials_error_is_clean_via_cli(
        self, source_env, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HEALTHMES_DATABASE_URL", source_env.database_url)
        monkeypatch.setenv("HEALTHMES_DATA_DIR", str(source_env.data_dir))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "backups"))
        monkeypatch.setenv("HEALTHMES_VAULT_BUCKET", BUCKET)
        monkeypatch.setenv("HEALTHMES_VAULT_ACCESS_KEY_ID", "wrong")
        monkeypatch.setenv("HEALTHMES_VAULT_SECRET_ACCESS_KEY", "wrong")
        monkeypatch.setenv("HEALTHMES_VAULT_REGION", "us-east-1")
        path = create_snapshot_via_cli(capsys)

        monkeypatch.setattr(boto3, "client", lambda *a, **k: _WrongCredentialsClient())
        assert main(["backup", "push", path]) == 1
        captured = capsys.readouterr()
        assert "error:" in captured.err
        assert "credentials" in captured.err
        assert "HEALTHMES_VAULT_ACCESS_KEY_ID" in captured.err
        assert "Traceback" not in captured.err
        assert "\n" not in captured.err.strip()  # single line

    def test_unknown_client_error_suppresses_provider_message(
        self,
        local_provider,
    ):
        provider = RemoteVaultProvider(
            make_config(
                endpoint_url=(
                    "https://alice:configured-password@example.invalid/"
                    "private?token=configured-token"
                ),
                access_key_id="CONFIGURED-ACCESS-KEY",
                secret_access_key="CONFIGURED-SECRET-KEY",
            ),
            local=local_provider,
        )
        leaked = (
            "https://bob:TOP-SECRET-CREDENTIAL@example.invalid/private"
            "?token=LEAKED-TOKEN#LEAKED-FRAGMENT\nsecond line"
        )

        with pytest.raises(BackupError) as excinfo:
            with provider._vault_call("list snapshots"):
                raise ClientError(
                    {
                        "Error": {
                            "Code": "InternalError",
                            "Message": leaked,
                        }
                    },
                    "ListObjectsV2",
                )

        rendered = str(excinfo.value)
        assert rendered == (
            "vault list snapshots failed (InternalError); check "
            "HEALTHMES_VAULT_ENDPOINT, HEALTHMES_VAULT_BUCKET, and "
            "provider status"
        )
        for secret in (
            leaked,
            "TOP-SECRET-CREDENTIAL",
            "LEAKED-TOKEN",
            "LEAKED-FRAGMENT",
            "configured-password",
            "configured-token",
            "CONFIGURED-ACCESS-KEY",
            "CONFIGURED-SECRET-KEY",
        ):
            assert secret not in rendered
        assert "\n" not in rendered

    def test_generic_vault_exception_suppresses_secret_text(
        self,
        local_provider,
        caplog,
    ):
        provider = RemoteVaultProvider(make_config(), local=local_provider)
        leaked = (
            "https://alice:TOP-SECRET-CREDENTIAL@example.invalid/"
            "private?token=LEAKED-TOKEN\nsecond line"
        )

        with pytest.raises(BackupError) as excinfo:
            with provider._vault_call("download snapshot"):
                raise RuntimeError(leaked)

        rendered = str(excinfo.value)
        assert rendered == (
            "vault download snapshot failed; check "
            "HEALTHMES_VAULT_ENDPOINT, HEALTHMES_VAULT_BUCKET, and "
            "provider status"
        )
        assert "TOP-SECRET-CREDENTIAL" not in rendered
        assert "LEAKED-TOKEN" not in rendered
        assert "\n" not in rendered
        assert "RuntimeError" in caplog.text
        assert "TOP-SECRET-CREDENTIAL" not in caplog.text
        assert "LEAKED-TOKEN" not in caplog.text

    def test_missing_bucket_error_is_actionable(self, s3, local_provider):
        provider = RemoteVaultProvider(make_config(bucket="no-such-vault"), local=local_provider)
        info = provider.local.export_snapshot()
        with pytest.raises(BackupError, match="vault bucket not found: 'no-such-vault'"):
            provider.push(info.path)

    def test_unconfigured_vault_from_settings_is_refused(self, tmp_path):
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'db.sqlite3'}",
            data_dir=tmp_path / "data",
            scheduler_enabled=False,
            _env_file=None,
        )
        with pytest.raises(BackupError, match="HEALTHMES_VAULT_BUCKET"):
            RemoteVaultProvider.from_settings(settings)

    def test_upload_integrity_mismatch_deletes_only_exact_put_generation(
        self,
        local_provider,
    ):
        deleted: list[dict[str, str]] = []
        reads: list[dict[str, str]] = []
        info = local_provider.export_snapshot()
        corrupt = b"x" * info.size_bytes

        def get_object(**kwargs):
            reads.append(kwargs)
            return {
                "Body": io.BytesIO(corrupt),
                "ContentLength": len(corrupt),
                "Metadata": {},
                "ETag": "uploaded-etag",
                "VersionId": "uploaded-v1",
            }

        fake = SimpleNamespace(
            put_object=lambda **kw: {
                "ETag": "uploaded-etag",
                "VersionId": "uploaded-v1",
            },
            get_object=get_object,
            delete_object=lambda **kw: deleted.append(kw),
        )
        provider = RemoteVaultProvider(make_config(), local=local_provider)
        provider._s3 = fake  # bypass boto3 construction; verify our own check
        with pytest.raises(BackupError, match="integrity check failed"):
            provider.push(info.path)
        assert reads == [
            {
                "Bucket": BUCKET,
                "Key": f"{PREFIX}/{info.name}",
                "VersionId": "uploaded-v1",
            }
        ]
        assert deleted == [
            {
                "Bucket": BUCKET,
                "Key": f"{PREFIX}/{info.name}",
                "VersionId": "uploaded-v1",
            }
        ]

    def test_corrupt_generation_delete_cleanup_redacts_secrets(
        self,
        local_provider,
        caplog,
    ):
        info = local_provider.export_snapshot()
        corrupt = b"x" * info.size_bytes
        leaked = (
            "https://alice:DELETE-SECRET@example.invalid/private?token="
            "DELETE-TOKEN\nsecond line"
        )

        class DeleteFailingClient:
            def put_object(self, **_kwargs):
                return {
                    "ETag": "uploaded-etag",
                    "VersionId": "uploaded-v1",
                }

            def get_object(self, **_kwargs):
                return {
                    "Body": io.BytesIO(corrupt),
                    "ContentLength": len(corrupt),
                    "Metadata": {},
                    "ETag": "uploaded-etag",
                    "VersionId": "uploaded-v1",
                }

            def delete_object(self, **_kwargs):
                raise RuntimeError(leaked)

        provider = RemoteVaultProvider(make_config(), local=local_provider)
        provider._s3 = DeleteFailingClient()

        with (
            caplog.at_level(
                "WARNING",
                logger="healthmes.backup.remote_vault",
            ),
            pytest.raises(BackupError, match="integrity check failed") as excinfo,
        ):
            provider.push(info.path)

        assert "cleanup of the exact uploaded generation failed" in str(excinfo.value)
        assert "action=delete corrupt vault object generation" in caplog.text
        assert "error_type=RuntimeError" in caplog.text
        assert "provider_detail=suppressed" in caplog.text
        for secret in ("DELETE-SECRET", "DELETE-TOKEN", leaked):
            assert secret not in caplog.text
            assert secret not in str(excinfo.value)

    def test_verification_body_close_failure_is_redacted_and_push_succeeds(
        self,
        local_provider,
        caplog,
    ):
        info = local_provider.export_snapshot()
        original = info.path.read_bytes()
        leaked = (
            "https://alice:VERIFY-CLOSE-SECRET@example.invalid/private?token="
            "VERIFY-CLOSE-TOKEN"
        )

        class CloseFailingVerificationClient:
            def __init__(self) -> None:
                self.uploaded = b""
                self.body: _CloseFailingBody | None = None

            def put_object(self, **kwargs):
                self.uploaded = kwargs["Body"].read()
                return {
                    "ETag": "uploaded-etag",
                    "VersionId": "uploaded-v1",
                }

            def get_object(self, **_kwargs):
                self.body = _CloseFailingBody(self.uploaded, leaked)
                return {
                    "Body": self.body,
                    "ContentLength": len(self.uploaded),
                    "Metadata": {},
                    "ETag": "uploaded-etag",
                    "VersionId": "uploaded-v1",
                }

        client = CloseFailingVerificationClient()
        provider = RemoteVaultProvider(make_config(), local=local_provider)
        provider._s3 = client

        with caplog.at_level(
            "WARNING",
            logger="healthmes.backup.remote_vault",
        ):
            pushed = provider.push(info.path)

        assert pushed.name == info.name
        assert client.uploaded == original
        assert client.body is not None
        assert client.body.close_calls == 1
        assert "action=close vault verification body" in caplog.text
        assert "error_type=RuntimeError" in caplog.text
        assert "provider_detail=suppressed" in caplog.text
        for secret in ("VERIFY-CLOSE-SECRET", "VERIFY-CLOSE-TOKEN", leaked):
            assert secret not in caplog.text

    def test_download_body_close_failure_is_redacted_and_download_succeeds(
        self,
        local_provider,
        caplog,
    ):
        info = local_provider.export_snapshot()
        payload = info.path.read_bytes()
        info.path.unlink()
        leaked = (
            "https://alice:DOWNLOAD-CLOSE-SECRET@example.invalid/private?token="
            "DOWNLOAD-CLOSE-TOKEN"
        )
        body = _CloseFailingBody(payload, leaked)
        provider = RemoteVaultProvider(make_config(), local=local_provider)
        provider._s3 = SimpleNamespace(
            get_object=lambda **_kwargs: {
                "Body": body,
                "ContentLength": len(payload),
                "Metadata": {"healthmes-sha256": hashlib.sha256(payload).hexdigest()},
                "ETag": "",
            }
        )

        with caplog.at_level(
            "WARNING",
            logger="healthmes.backup.remote_vault",
        ):
            downloaded = provider.download(info.name)

        assert downloaded == info.path
        assert downloaded.read_bytes() == payload
        assert body.close_calls == 1
        assert "action=close vault download body" in caplog.text
        assert "error_type=RuntimeError" in caplog.text
        assert "provider_detail=suppressed" in caplog.text
        for secret in (
            "DOWNLOAD-CLOSE-SECRET",
            "DOWNLOAD-CLOSE-TOKEN",
            leaked,
        ):
            assert secret not in caplog.text

    def test_non_versioned_corruption_keeps_local_and_deletes_nothing(
        self,
        source_env,
        tmp_path,
    ):
        class CorruptingClient:
            def __init__(self) -> None:
                self.remote = b""
                self.deleted: list[str] = []

            def put_object(self, **kwargs):
                original = kwargs["Body"].read()
                self.remote = bytes([original[0] ^ 1]) + original[1:]
                return {
                    "ETag": "multipart-etag",
                    "VersionId": "null",
                }

            def get_object(self, **_kwargs):
                return {
                    "Body": io.BytesIO(self.remote),
                    "ContentLength": len(self.remote),
                    "Metadata": {},
                    "ETag": "multipart-etag",
                    "VersionId": "null",
                }

            def delete_object(self, **kwargs):
                self.deleted.append(kwargs["Key"])

        local = LocalDirectoryProvider(
            tmp_path / "remote-only-corruption",
            locations=source_env.locations,
            passphrase=source_env.passphrase,
            clock=lambda: T1,
        )
        provider = RemoteVaultProvider(
            make_config(),
            local=local,
            keep_local=False,
        )
        client = CorruptingClient()
        provider._s3 = client

        with pytest.raises(BackupError, match="integrity check failed"):
            provider.export_snapshot()

        local_snapshots = list(local.backup_dir.glob("*.tar.gz.age"))
        assert len(local_snapshots) == 1
        assert local_snapshots[0].stat().st_size == len(client.remote)
        assert client.deleted == []

    def test_remote_only_verifies_and_removes_using_exact_put_generation(
        self,
        local_provider,
    ):
        class RacingVersionedClient:
            def __init__(self) -> None:
                self.uploaded = b""
                self.current = b"newer unrelated generation"
                self.reads: list[dict[str, str]] = []

            def put_object(self, **kwargs):
                self.uploaded = kwargs["Body"].read()
                return {
                    "ETag": "uploaded-etag",
                    "VersionId": "uploaded-v1",
                }

            def get_object(self, **kwargs):
                self.reads.append(kwargs)
                assert self.current != self.uploaded
                return {
                    "Body": io.BytesIO(self.uploaded),
                    "ContentLength": len(self.uploaded),
                    "Metadata": {},
                    "ETag": "uploaded-etag",
                    "VersionId": "uploaded-v1",
                }

        client = RacingVersionedClient()
        provider = RemoteVaultProvider(
            make_config(),
            local=local_provider,
            keep_local=False,
        )
        provider._s3 = client

        info = provider.export_snapshot()

        assert info.version_id == "uploaded-v1"
        assert client.reads == [
            {
                "Bucket": BUCKET,
                "Key": f"{PREFIX}/{info.name}",
                "VersionId": "uploaded-v1",
            }
        ]
        assert list(local_provider.backup_dir.glob("*.tar.gz.age")) == []
        assert client.current == b"newer unrelated generation"

    def test_remote_only_rejects_get_that_is_not_bound_to_put_generation(
        self,
        local_provider,
    ):
        deleted: list[dict[str, str]] = []

        class MisbindingClient:
            def __init__(self) -> None:
                self.uploaded = b""

            def put_object(self, **kwargs):
                self.uploaded = kwargs["Body"].read()
                return {
                    "ETag": "uploaded-etag",
                    "VersionId": "uploaded-v1",
                }

            def get_object(self, **kwargs):
                assert kwargs["VersionId"] == "uploaded-v1"
                return {
                    "Body": io.BytesIO(self.uploaded),
                    "ContentLength": len(self.uploaded),
                    "Metadata": {},
                    "ETag": "newer-etag",
                    "VersionId": "newer-v2",
                }

            def delete_object(self, **kwargs):
                deleted.append(kwargs)

        provider = RemoteVaultProvider(
            make_config(),
            local=local_provider,
            keep_local=False,
        )
        provider._s3 = MisbindingClient()

        with pytest.raises(
            BackupError,
            match="did not return the exact uploaded object generation",
        ):
            provider.export_snapshot()

        assert len(list(local_provider.backup_dir.glob("*.tar.gz.age"))) == 1
        assert deleted == []

    def test_remote_only_preserves_a_file_replaced_after_upload(
        self,
        s3,
        local_provider,
        monkeypatch,
    ):
        enable_versioning(s3)
        provider = RemoteVaultProvider(
            make_config(),
            local=local_provider,
            keep_local=False,
        )
        real_remove = local_provider.remove_snapshot_if_unchanged

        def replace_before_remove(path, *, expected):
            path.unlink()
            path.write_bytes(b"new file at the old snapshot path")
            return real_remove(path, expected=expected)

        monkeypatch.setattr(
            local_provider,
            "remove_snapshot_if_unchanged",
            replace_before_remove,
        )

        with pytest.raises(BackupError, match="changed after upload"):
            provider.export_snapshot()

        local_snapshots = list(local_provider.backup_dir.glob("*.tar.gz.age"))
        assert len(local_snapshots) == 1
        assert local_snapshots[0].read_bytes() == b"new file at the old snapshot path"
        assert vault_keys(s3) == [f"{PREFIX}/{local_snapshots[0].name}"]


# ---------------------------------------------------------------------------
# Settings / selector resolution
# ---------------------------------------------------------------------------


class TestSettingsResolution:
    def test_unconfigured_means_none(self, tmp_path):
        settings = Settings(
            database_url="sqlite:///x.db", data_dir=tmp_path, _env_file=None
        )
        assert resolve_vault_config(settings) is None

    def test_env_fallback_builds_full_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTHMES_VAULT_ENDPOINT", "http://localhost:9000")
        monkeypatch.setenv("HEALTHMES_VAULT_BUCKET", "bkt")
        monkeypatch.setenv("HEALTHMES_VAULT_ACCESS_KEY_ID", "ak")
        monkeypatch.setenv("HEALTHMES_VAULT_SECRET_ACCESS_KEY", "sk")
        monkeypatch.setenv("HEALTHMES_VAULT_REGION", "auto")
        monkeypatch.setenv("HEALTHMES_VAULT_PREFIX", "/vaults/me/")
        settings = Settings(
            database_url="sqlite:///x.db", data_dir=tmp_path, _env_file=None
        )
        config = resolve_vault_config(settings)
        assert config == VaultConfig(
            bucket="bkt",
            endpoint_url="http://localhost:9000",
            access_key_id="ak",
            secret_access_key="sk",
            region="auto",
            prefix="vaults/me",  # normalized
        )

    def test_default_prefix_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTHMES_VAULT_BUCKET", "bkt")
        settings = Settings(
            database_url="sqlite:///x.db", data_dir=tmp_path, _env_file=None
        )
        config = resolve_vault_config(settings)
        assert config is not None
        assert config.prefix == DEFAULT_VAULT_PREFIX

    def test_settings_attributes_win_over_env(self, monkeypatch):
        monkeypatch.setenv("HEALTHMES_VAULT_BUCKET", "from-env")
        double = SimpleNamespace(
            vault_bucket="from-settings",
            vault_endpoint=None,
            vault_access_key_id=None,
            vault_secret_access_key=SecretStr("s3cret"),
            vault_region=None,
            vault_prefix=None,
        )
        config = resolve_vault_config(double)
        assert config is not None
        assert config.bucket == "from-settings"
        assert config.secret_access_key == "s3cret"  # SecretStr unwrapped

    def test_secret_is_not_in_repr(self):
        config = make_config(secret_access_key="super-secret")
        assert "super-secret" not in repr(config)

    def test_provider_selector_default_alias_and_invalid(self, tmp_path, monkeypatch):
        settings = Settings(
            database_url="sqlite:///x.db", data_dir=tmp_path, _env_file=None
        )
        assert resolve_backup_provider_name(settings) == "local"
        monkeypatch.setenv("HEALTHMES_BACKUP_PROVIDER", "remote_vault")
        assert resolve_backup_provider_name(settings) == "remote_vault"
        monkeypatch.setenv("HEALTHMES_BACKUP_PROVIDER", "remote")  # CLI-flag alias
        assert resolve_backup_provider_name(settings) == "remote_vault"
        monkeypatch.setenv("HEALTHMES_BACKUP_PROVIDER", "carrier-pigeon")
        with pytest.raises(BackupError, match="unknown backup provider"):
            resolve_backup_provider_name(settings)


# ---------------------------------------------------------------------------
# Weekly job replication (selector-aware, still never raises)
# ---------------------------------------------------------------------------


def make_settings(source_env) -> Settings:
    return Settings(
        database_url=source_env.database_url,
        data_dir=source_env.data_dir,
        scheduler_enabled=False,
        _env_file=None,
    )


class TestWeeklyJobReplication:
    def test_replicates_when_selector_is_remote_vault(
        self, source_env, tmp_path, monkeypatch, s3
    ):
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "weekly"))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HEALTHMES_BACKUP_PROVIDER", "remote_vault")
        monkeypatch.setenv("HEALTHMES_VAULT_BUCKET", BUCKET)
        monkeypatch.setenv("HEALTHMES_VAULT_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("HEALTHMES_VAULT_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("HEALTHMES_VAULT_REGION", "us-east-1")
        monkeypatch.setenv("HEALTHMES_VAULT_PREFIX", PREFIX)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)

        build_backup_job(make_settings(source_env))()

        local = list((tmp_path / "weekly").glob("healthmes-backup-*.tar.gz.age"))
        assert len(local) == 1  # local copy always kept by the weekly job
        assert vault_keys(s3) == [f"{PREFIX}/{local[0].name}"]

    def test_failed_replication_keeps_local_and_never_raises(
        self, source_env, tmp_path, monkeypatch, s3, caplog
    ):
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "weekly"))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HEALTHMES_BACKUP_PROVIDER", "remote_vault")
        monkeypatch.setenv("HEALTHMES_VAULT_BUCKET", "bucket-that-does-not-exist")
        monkeypatch.setenv("HEALTHMES_VAULT_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("HEALTHMES_VAULT_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("HEALTHMES_VAULT_REGION", "us-east-1")
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)

        with caplog.at_level("ERROR", logger="healthmes.backup.local"):
            build_backup_job(make_settings(source_env))()  # must not raise

        assert len(list((tmp_path / "weekly").glob("*.tar.gz.age"))) == 1
        assert "vault replication failed" in caplog.text

    def test_local_selector_never_touches_the_vault(self, source_env, tmp_path, monkeypatch, s3):
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "weekly"))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HEALTHMES_VAULT_BUCKET", BUCKET)  # configured but not selected
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)

        build_backup_job(make_settings(source_env))()
        assert vault_keys(s3) == []
