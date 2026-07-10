"""CLI tests: ``python -m healthmes [serve|backup create/list/restore]``.

Settings are driven purely through env vars + a tmp cwd (so no repo ``.env``
leaks in); the serve path is asserted without binding a socket by stubbing
``uvicorn.run``.
"""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from healthmes.__main__ import main


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


class TestBackupRestore:
    def test_restore_without_yes_is_a_dry_run(self, cli_env, capsys):
        path = create_snapshot_via_cli(capsys)
        marker = cli_env.media_dir / "note.txt"
        marker.write_text("mutated after snapshot", encoding="utf-8")

        assert main(["backup", "restore", path]) == 2
        captured = capsys.readouterr()
        assert "healthmes db:       sqlite_file" in captured.out
        assert "re-run with --yes" in captured.err
        # Dry run must not touch live data.
        assert marker.read_text(encoding="utf-8") == "mutated after snapshot"

    def test_restore_with_yes_applies(self, cli_env, capsys):
        original = (cli_env.media_dir / "note.txt").read_bytes()
        path = create_snapshot_via_cli(capsys)
        (cli_env.media_dir / "note.txt").write_text("mutated", encoding="utf-8")
        (cli_env.media_dir / "extra.bin").write_bytes(b"junk")

        assert main(["backup", "restore", path, "--yes"]) == 0
        assert "restored:" in capsys.readouterr().out
        assert (cli_env.media_dir / "note.txt").read_bytes() == original
        assert not (cli_env.media_dir / "extra.bin").exists()

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
