"""Fixtures for the backup test suite.

Everything runs on tmp dirs with sqlite, fake media and a fake HERMES_HOME —
no network, no Docker, no postgres (pg_dump/pg_restore paths are exercised
through stub executables inside the tests themselves).
"""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from healthmes.backup.snapshot import DataLocations

PASSPHRASE = "test-passphrase-42"

# Deterministic binary payload with non-UTF8 bytes (a fake photo).
FAKE_JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 3

# Remote-vault env vars that must never leak from the developer's shell into
# the suite (a stray HEALTHMES_BACKUP_PROVIDER=remote_vault would flip every
# CLI test onto the vault path).
_VAULT_ENV_VARS = (
    "HEALTHMES_BACKUP_PROVIDER",
    "HEALTHMES_VAULT_ENDPOINT",
    "HEALTHMES_VAULT_BUCKET",
    "HEALTHMES_VAULT_ACCESS_KEY_ID",
    "HEALTHMES_VAULT_SECRET_ACCESS_KEY",
    "HEALTHMES_VAULT_REGION",
    "HEALTHMES_VAULT_PREFIX",
)


@pytest.fixture(autouse=True)
def _isolate_vault_env(monkeypatch):
    for name in _VAULT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@dataclass(frozen=True)
class SourceEnv:
    """A complete fake live environment to snapshot."""

    root: Path
    data_dir: Path
    db_path: Path
    database_url: str
    media_dir: Path
    hermes_home: Path
    outside_skill: Path  # target of the out-of-tree symlink under hermes/skills
    locations: DataLocations
    passphrase: str


def _make_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE task (id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT)"
        )
        connection.executemany(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            [("write weekly plan", "todo"), ("log lunch", "done"), ("HRV review", "todo")],
        )
        connection.commit()
    finally:
        connection.close()


def build_source_env(root: Path, passphrase: str = PASSPHRASE) -> SourceEnv:
    """Materialize the fake live environment under ``root``."""
    data_dir = root / "data"
    db_path = data_dir / "healthmes.db"
    _make_sqlite_db(db_path)

    media_dir = data_dir / "media"
    (media_dir / "food").mkdir(parents=True)
    (media_dir / "food" / "pancakes.jpg").write_bytes(FAKE_JPEG)
    (media_dir / "note.txt").write_text("voice memo transcript\n", encoding="utf-8")
    (media_dir / "archive").mkdir()  # empty dir must survive the round trip

    hermes_home = root / "hermes_home"
    (hermes_home / "memory").mkdir(parents=True)
    (hermes_home / "config.yaml").write_text("agents: {}\n", encoding="utf-8")
    (hermes_home / "memory" / "state.json").write_text('{"facts": [1, 2]}', encoding="utf-8")
    # Intra-tree relative symlink: preserved as a symlink in the envelope.
    os.symlink("state.json", hermes_home / "memory" / "current.json")
    (hermes_home / "cron").mkdir()  # empty dir
    # Out-of-tree absolute symlink (bootstrap-managed skill link): skipped + recorded.
    outside_skill = root / "outside-skill"
    outside_skill.mkdir()
    (outside_skill / "SKILL.md").write_text("# planner\n", encoding="utf-8")
    (hermes_home / "skills").mkdir()
    os.symlink(str(outside_skill), hermes_home / "skills" / "healthmes-planner")

    return SourceEnv(
        root=root,
        data_dir=data_dir,
        db_path=db_path,
        database_url=f"sqlite:///{db_path}",
        media_dir=media_dir,
        hermes_home=hermes_home,
        outside_skill=outside_skill,
        locations=DataLocations(
            database_url=f"sqlite:///{db_path}",
            ow_database_url=None,
            media_dir=media_dir,
            hermes_home=hermes_home,
        ),
        passphrase=passphrase,
    )


@pytest.fixture
def source_env(tmp_path: Path) -> SourceEnv:
    return build_source_env(tmp_path / "source")


@pytest.fixture
def fresh_locations(tmp_path: Path):
    """Factory for empty restore targets (nothing exists until restore runs)."""

    def _make(name: str = "target") -> tuple[DataLocations, Path]:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        locations = DataLocations(
            database_url=f"sqlite:///{root / 'data' / 'healthmes.db'}",
            ow_database_url=None,
            media_dir=root / "data" / "media",
            hermes_home=root / "hermes_home",
        )
        return locations, root

    return _make


def dump_sqlite(path: Path) -> tuple[list[str], list[tuple]]:
    """Logical content of a sqlite db (schema SQL + task rows).

    Snapshots go through ``sqlite3.Connection.backup`` (consistent even
    against live writers), which is logically exact but not byte-identical
    (header change counters differ) — so round-trip tests compare content,
    not bytes.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        schema = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        ]
        rows = list(connection.execute("SELECT id, title, status FROM task ORDER BY id"))
    finally:
        connection.close()
    return schema, rows


@pytest.fixture
def sqlite_dump():
    return dump_sqlite


def snapshot_tree(root: Path) -> dict[str, tuple]:
    """Byte-exact structural snapshot of a tree (files, dirs, symlinks)."""
    entries: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            entries[rel] = ("dir",)
        elif path.is_file():
            entries[rel] = ("file", path.read_bytes())
    return entries


@pytest.fixture
def tree_snapshot():
    return snapshot_tree
