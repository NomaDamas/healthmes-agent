"""CLI tests: ``python -m healthmes [serve|backup create/list/restore]``.

Settings are driven purely through env vars + a tmp cwd (so no repo ``.env``
leaks in); the serve path is asserted without binding a socket by stubbing
``uvicorn.run``.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from healthmes import __main__ as cli_mod
from healthmes.__main__ import main
from healthmes.backup.local import LocalDirectoryProvider


@pytest.fixture
def cli_env(source_env, tmp_path, monkeypatch):
    """Fake live environment exposed to the CLI via env vars only."""
    monkeypatch.chdir(tmp_path)  # no repo .env in reach
    monkeypatch.setenv("HEALTHMES_DATABASE_URL", source_env.database_url)
    monkeypatch.setenv("HEALTHMES_DATA_DIR", str(source_env.data_dir))
    monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
    monkeypatch.setenv("HERMES_HOME", str(source_env.hermes_home))
    monkeypatch.delenv("HEALTHMES_BACKUP_DIR", raising=False)
    monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
    return source_env


def create_snapshot_via_cli(capsys) -> str:
    assert main(["backup", "create"]) == 0
    out = capsys.readouterr().out
    assert "snapshot written:" in out
    return out.split("snapshot written:")[1].split("(")[0].strip()


class TestBackupCreateAndList:
    def test_create_writes_into_default_backup_dir(self, cli_env, capsys):
        path = create_snapshot_via_cli(capsys)
        assert path.startswith(str(cli_env.data_dir / "backups"))
        assert path.endswith(".tar.gz.age")

    def test_create_warns_when_runtime_ow_dump_is_absent(
        self, cli_env, capsys, monkeypatch, caplog
    ):
        monkeypatch.setenv("HEALTHMES_OW_API_KEY", "runtime-key")
        with caplog.at_level("WARNING", logger="healthmes.backup.snapshot"):
            path = create_snapshot_via_cli(capsys)

        assert path.endswith(".tar.gz.age")
        assert "Partial backup" in caplog.text
        assert "HEALTHMES_OW_DATABASE_URL is unset" in caplog.text

    def test_list_shows_snapshots_without_passphrase(self, cli_env, capsys, monkeypatch):
        create_snapshot_via_cli(capsys)
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE")
        assert main(["backup", "list"]) == 0
        out = capsys.readouterr().out
        assert "healthmes-backup-" in out
        assert out.count("\n") == 1

    def test_list_empty_dir(self, cli_env, capsys):
        assert main(["backup", "list"]) == 0
        assert "no snapshots" in capsys.readouterr().out

    def test_create_without_passphrase_fails_cleanly(self, cli_env, capsys, monkeypatch):
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE")
        assert main(["backup", "create"]) == 1
        captured = capsys.readouterr()
        assert "error:" in captured.err
        assert "HEALTHMES_BACKUP_PASSPHRASE" in captured.err

    def test_passphrase_file_overrides_env(self, cli_env, capsys, monkeypatch, tmp_path):
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE")
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text(cli_env.passphrase + "\n", encoding="utf-8")
        assert main(["backup", "create", "--passphrase-file", str(secret_file)]) == 0
        assert "snapshot written:" in capsys.readouterr().out

    def test_empty_passphrase_file_rejected(self, cli_env, capsys, monkeypatch, tmp_path):
        secret_file = tmp_path / "empty.txt"
        secret_file.write_text("\n", encoding="utf-8")
        assert main(["backup", "create", "--passphrase-file", str(secret_file)]) == 1
        assert "empty" in capsys.readouterr().err

    def test_create_output_io_failure_follows_backup_error_contract(
        self,
        cli_env,
        capsys,
        monkeypatch,
        tmp_path,
    ):
        blocked_backup_dir = tmp_path / "backup-dir-is-a-file"
        blocked_backup_dir.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(blocked_backup_dir))

        assert main(["backup", "create"]) == 1

        captured = capsys.readouterr()
        assert "error: could not write encrypted snapshot" in captured.err
        assert "Traceback" not in captured.err


class TestBackupPush:
    def test_passphrase_file_is_forwarded_to_push_provider(
        self,
        cli_env,
        capsys,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE")
        secret_file = tmp_path / "vault-passphrase.txt"
        secret_file.write_text(cli_env.passphrase + "\n", encoding="utf-8")
        observed: dict[str, str | None] = {}

        class FakeVault:
            @staticmethod
            def object_uri(name):
                return f"s3://test/{name}"

            @staticmethod
            def push(snapshot):
                observed["snapshot"] = str(snapshot)
                return SimpleNamespace(name="snapshot.age", size_bytes=17)

        def fake_vault_provider(args, settings, *, keep_local=True):
            observed["passphrase"] = cli_mod._passphrase_from(args, settings)
            return FakeVault()

        monkeypatch.setattr(cli_mod, "_vault_provider", fake_vault_provider)

        assert (
            main(
                [
                    "backup",
                    "push",
                    "snapshot.age",
                    "--passphrase-file",
                    str(secret_file),
                ]
            )
            == 0
        )
        assert observed == {
            "passphrase": cli_env.passphrase,
            "snapshot": "snapshot.age",
        }
        assert "pushed: snapshot.age" in capsys.readouterr().out

    def test_empty_passphrase_file_rejects_push_before_provider_use(
        self,
        cli_env,
        capsys,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE")
        secret_file = tmp_path / "empty-vault-passphrase.txt"
        secret_file.write_text("\n", encoding="utf-8")
        provider_used = False

        def fake_vault_provider(args, settings, *, keep_local=True):
            nonlocal provider_used
            provider_used = True
            cli_mod._passphrase_from(args, settings)
            pytest.fail("empty passphrase must fail before provider creation")

        monkeypatch.setattr(cli_mod, "_vault_provider", fake_vault_provider)

        assert (
            main(
                [
                    "backup",
                    "push",
                    "snapshot.age",
                    "--passphrase-file",
                    str(secret_file),
                ]
            )
            == 1
        )
        assert provider_used is True
        assert "passphrase file is empty" in capsys.readouterr().err


class TestBackupRestore:
    def test_restore_without_yes_is_a_dry_run(self, cli_env, capsys):
        path = create_snapshot_via_cli(capsys)
        marker = cli_env.media_dir / "note.txt"
        marker.write_text("mutated after snapshot", encoding="utf-8")

        assert main(["backup", "restore", path]) == 2
        captured = capsys.readouterr()
        assert "healthmes db:       sqlite_file" in captured.out
        assert "recovery scope:     partial_component_snapshot" in captured.out
        assert "full-node recovery: no" in captured.out
        assert "raw ingest:" in captured.out
        assert "re-run with --yes" in captured.err
        # Dry run must not touch live data.
        assert marker.read_text(encoding="utf-8") == "mutated after snapshot"

    def test_restore_dry_run_uses_provider_resource_limits(
        self,
        cli_env,
        capsys,
        monkeypatch,
    ):
        path = create_snapshot_via_cli(capsys)
        configured_limit = 1024 * 1024
        monkeypatch.setenv(
            "HEALTHMES_BACKUP_MAX_ENCRYPTED_BYTES",
            str(configured_limit),
        )
        original_read_manifest = cli_mod.read_manifest
        observed_limits = []

        def tracked_read_manifest(snapshot, passphrase, *, limits=None):
            observed_limits.append(limits)
            return original_read_manifest(
                snapshot,
                passphrase,
                limits=limits,
            )

        monkeypatch.setattr(cli_mod, "read_manifest", tracked_read_manifest)

        assert main(["backup", "restore", path]) == 2
        capsys.readouterr()

        assert len(observed_limits) == 1
        assert observed_limits[0].max_encrypted_bytes == configured_limit

    def test_restore_dry_run_prints_reproducible_apply_command(
        self,
        cli_env,
        capsys,
        monkeypatch,
        tmp_path,
    ):
        path = create_snapshot_via_cli(capsys)
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE")
        secret_file = tmp_path / "secret with spaces.txt"
        secret_file.write_text(cli_env.passphrase + "\n", encoding="utf-8")

        assert (
            main(
                [
                    "backup",
                    "restore",
                    path,
                    "--provider",
                    "local",
                    "--passphrase-file",
                    str(secret_file),
                    "--allow-cross-store-partial",
                ]
            )
            == 2
        )

        command = capsys.readouterr().err
        assert "--provider local" in command
        assert f"--passphrase-file '{secret_file}'" in command
        assert "--allow-cross-store-partial" in command
        assert command.rstrip().endswith("--yes")

    def test_restore_dry_run_preserves_python_module_launcher(
        self,
        cli_env,
        capsys,
        monkeypatch,
    ):
        path = create_snapshot_via_cli(capsys)
        monkeypatch.setattr(
            cli_mod.sys,
            "orig_argv",
            [cli_mod.sys.executable, "-m", "healthmes", "backup", "restore"],
        )

        assert main(["backup", "restore", path]) == 2

        command = capsys.readouterr().err
        assert " -m healthmes backup restore " in command
        assert command.rstrip().endswith("--yes")

    def test_restore_dry_run_reuses_available_console_launcher(
        self,
        cli_env,
        capsys,
        monkeypatch,
    ):
        path = create_snapshot_via_cli(capsys)
        console_script = str(
            Path(cli_mod.sys.executable).parent / "healthmes"
        )
        monkeypatch.setattr(
            cli_mod.sys,
            "orig_argv",
            [
                cli_mod.sys.executable,
                console_script,
            ],
        )
        monkeypatch.setattr(
            cli_mod.sys,
            "argv",
            [console_script],
        )
        monkeypatch.setattr(
            cli_mod.shutil,
            "which",
            lambda launcher: (
                console_script if launcher == console_script else None
            ),
        )

        assert main(["backup", "restore", path]) == 2

        command = capsys.readouterr().err
        assert f"{console_script} backup restore " in command
        assert "uv run" not in command
        assert command.rstrip().endswith("--yes")

    def test_restore_dry_run_does_not_infer_uv_from_checkout_lock(
        self,
        cli_env,
        capsys,
        monkeypatch,
        tmp_path,
    ):
        path = create_snapshot_via_cli(capsys)
        unavailable_script = str(tmp_path / "bin" / "healthmes")
        monkeypatch.setattr(
            cli_mod.sys,
            "orig_argv",
            [cli_mod.sys.executable, unavailable_script],
        )
        monkeypatch.setattr(cli_mod.sys, "argv", [unavailable_script])
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _launcher: None)

        assert main(["backup", "restore", path]) == 2

        command = capsys.readouterr().err
        assert " -m healthmes backup restore " in command
        assert "uv run" not in command
        assert command.rstrip().endswith("--yes")

    def test_restore_with_yes_applies(self, cli_env, capsys):
        original = (cli_env.media_dir / "note.txt").read_bytes()
        path = create_snapshot_via_cli(capsys)
        (cli_env.media_dir / "note.txt").write_text("mutated", encoding="utf-8")
        (cli_env.media_dir / "extra.bin").write_bytes(b"junk")

        assert main(["backup", "restore", path, "--yes"]) == 0
        out = capsys.readouterr().out
        assert "restored:" in out
        assert "recovery mode: recoverable_local_swaps" in out
        assert "recovered: healthmes_db, media, hermes_home" in out
        assert "not in snapshot: open_wearables_db, raw_ingest" in out
        assert (cli_env.media_dir / "note.txt").read_bytes() == original
        assert not (cli_env.media_dir / "extra.bin").exists()

    def test_restore_forwards_explicit_cross_store_partial_flag(
        self,
        cli_env,
        capsys,
        monkeypatch,
    ):
        path = create_snapshot_via_cli(capsys)
        original_restore = LocalDirectoryProvider.restore
        accepted: list[bool] = []

        def tracked_restore(
            provider,
            snapshot,
            *,
            allow_cross_store_partial=False,
        ):
            accepted.append(allow_cross_store_partial)
            return original_restore(
                provider,
                snapshot,
                allow_cross_store_partial=allow_cross_store_partial,
            )

        monkeypatch.setattr(
            LocalDirectoryProvider,
            "restore",
            tracked_restore,
        )

        assert (
            main(
                [
                    "backup",
                    "restore",
                    path,
                    "--yes",
                    "--allow-cross-store-partial",
                ]
            )
            == 0
        )
        assert accepted == [True]

    def test_restore_accepts_bare_snapshot_name(self, cli_env, capsys):
        path = create_snapshot_via_cli(capsys)
        name = path.rsplit("/", 1)[1]
        assert main(["backup", "restore", name, "--yes"]) == 0

    def test_restore_with_wrong_passphrase_fails_cleanly(self, cli_env, capsys, monkeypatch):
        path = create_snapshot_via_cli(capsys)
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", "wrong")
        assert main(["backup", "restore", path, "--yes"]) == 1
        err = capsys.readouterr().err
        assert "error:" in err and "passphrase" in err
        assert "Traceback" not in err

    def test_restore_unknown_snapshot_fails(self, cli_env, capsys):
        assert main(["backup", "restore", "healthmes-backup-19700101T000000Z.tar.gz.age"]) == 1
        assert "snapshot not found" in capsys.readouterr().err


def _serve_settings(**overrides) -> SimpleNamespace:
    """Minimal Settings double for the serve path (host/token interlock)."""
    fields = {"port": 8123, "host": "127.0.0.1", "api_token": SecretStr("")}
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestServe:
    def test_bare_invocation_serves(self, monkeypatch):
        calls = {}

        def fake_run(app, **kwargs):
            calls["app"] = app
            calls.update(kwargs)

        monkeypatch.setattr("uvicorn.run", fake_run)
        monkeypatch.setattr(
            "healthmes.__main__.get_settings", lambda: _serve_settings(port=8123)
        )
        assert main([]) == 0
        assert calls == {
            "app": "healthmes.app:create_app",
            "factory": True,
            "host": "127.0.0.1",
            "port": 8123,
        }

    def test_explicit_serve_subcommand(self, monkeypatch):
        calls = {}
        monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.setdefault("app", app))
        monkeypatch.setattr(
            "healthmes.__main__.get_settings", lambda: _serve_settings(port=8100)
        )
        assert main(["serve"]) == 0
        assert calls["app"] == "healthmes.app:create_app"

    def test_non_loopback_bind_without_token_refuses(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "uvicorn.run",
            lambda *a, **kw: pytest.fail("uvicorn must not start on an unsafe bind"),
        )
        monkeypatch.setattr(
            "healthmes.__main__.get_settings",
            lambda: _serve_settings(host="0.0.0.0"),
        )
        assert main(["serve"]) == 1
        err = capsys.readouterr().err
        assert "HEALTHMES_API_TOKEN" in err

    def test_non_loopback_bind_with_token_serves(self, monkeypatch):
        calls = {}
        monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.update(kw))
        monkeypatch.setattr(
            "healthmes.__main__.get_settings",
            lambda: _serve_settings(host="0.0.0.0", api_token=SecretStr("tok")),
        )
        assert main(["serve"]) == 0
        assert calls["host"] == "0.0.0.0"
