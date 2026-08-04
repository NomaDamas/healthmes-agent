"""Regression coverage for shell-safe local dotenv loading."""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOADER = REPO_ROOT / "scripts" / "load_runtime_env.py"


def test_loader_preserves_literal_shell_syntax_and_file_precedence(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    base = tmp_path / ".env"
    override = tmp_path / ".env.local"
    base.write_text(
        f'HEALTHMES_BACKUP_PASSPHRASE="$(touch {marker})"\n'
        "HEALTHMES_PORT=8100\n"
        "EXPANSION=$HOME\n",
        encoding="utf-8",
    )
    override.write_text("HEALTHMES_PORT=8123\n", encoding="utf-8")

    result = subprocess.run(
        [os.fspath(REPO_ROOT / ".venv/bin/python"), os.fspath(LOADER), base, override],
        check=True,
        capture_output=True,
    )
    fields = result.stdout.split(b"\0")
    loaded = dict(zip(fields[0::2], fields[1::2], strict=False))

    assert loaded[b"HEALTHMES_BACKUP_PASSPHRASE"] == f"$(touch {marker})".encode()
    assert loaded[b"HEALTHMES_PORT"] == b"8123"
    assert loaded[b"EXPANSION"] == b"$HOME"
    assert not marker.exists()
