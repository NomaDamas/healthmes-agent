from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from healthmes.store.enums import CalendarSource


def _path(data_dir: Path) -> Path:
    return data_dir / "runtime" / "calendar-status.json"


@contextmanager
def _status_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def record_calendar_status(
    data_dir: Path,
    source: CalendarSource,
    *,
    mode: str,
    error: Exception | None = None,
) -> None:
    path = _path(data_dir)
    with _status_lock(path):
        current = read_calendar_status(data_dir)
        current[source.value] = {
            "state": "error" if error is not None else "ok",
            "mode": mode,
            "updated_at": datetime.now(UTC).isoformat(),
            "error_type": type(error).__name__ if error is not None else "",
        }
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(current, stream, sort_keys=True)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def read_calendar_status(data_dir: Path) -> dict[str, dict[str, str]]:
    try:
        raw: Any = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(source): {str(key): str(value) for key, value in entry.items()}
        for source, entry in raw.items()
        if isinstance(entry, dict)
    }
