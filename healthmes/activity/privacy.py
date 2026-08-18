"""Source-side collection controls shared by device adapters and ingest."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from healthmes import clock
from healthmes.activity.contracts import (
    ActivityPermissionStatus,
    ActivityRecord,
    AppHourRecord,
    AppIntervalRecord,
)

BLOCKED_PERMISSION_STATES = {
    ActivityPermissionStatus.DENIED.value,
    ActivityPermissionStatus.REVOKED.value,
    ActivityPermissionStatus.UNAVAILABLE.value,
}

CONTROLLED_CATEGORIES = frozenset(
    {
        "accessibility",
        "audio",
        "browser",
        "communication",
        "desktop",
        "development",
        "education",
        "entertainment",
        "finance",
        "fitness",
        "game",
        "health",
        "image",
        "maps",
        "navigation",
        "news",
        "other",
        "productivity",
        "research",
        "shopping",
        "social",
        "system",
        "travel",
        "uncategorized",
        "utilities",
        "video",
    }
)

CATEGORY_ALIASES = {
    "games": "game",
    "map": "maps",
    "utility": "utilities",
}


def normalized_app_id(value: str) -> str:
    return value.strip().casefold()


def normalized_category(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().casefold().replace("_", "-").replace(" ", "-")
    normalized = CATEGORY_ALIASES.get(normalized, normalized)
    return normalized if normalized in CONTROLLED_CATEGORIES else "other"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CollectionGate:
    allowed: bool
    reason: str | None


def collection_gate(
    state: dict,
    *,
    now: datetime | None = None,
) -> CollectionGate:
    current = _as_utc(now) or clock.utc_now()
    if not bool(state.get("enabled", True)):
        return CollectionGate(False, "collection_disabled")
    permission = str(state.get("permission_status", "unknown"))
    if permission in BLOCKED_PERMISSION_STATES:
        return CollectionGate(False, f"permission_{permission}")
    paused_until_raw = state.get("paused_until")
    if isinstance(paused_until_raw, str):
        try:
            paused_until = _as_utc(datetime.fromisoformat(paused_until_raw))
        except ValueError:
            paused_until = None
    elif isinstance(paused_until_raw, datetime):
        paused_until = _as_utc(paused_until_raw)
    else:
        paused_until = None
    if paused_until is not None and paused_until > current:
        return CollectionGate(False, "collection_paused")
    return CollectionGate(True, None)


def record_app_id(record: ActivityRecord) -> str | None:
    if isinstance(record, AppHourRecord):
        return record.app_id
    if isinstance(record, AppIntervalRecord):
        return record.app_id
    return None


def filter_records(
    records: Iterable[ActivityRecord],
    state: dict,
    *,
    now: datetime | None = None,
) -> tuple[list[ActivityRecord], int, CollectionGate]:
    """Filter before persistence; device collectors can reuse the same contract."""
    gate = collection_gate(state, now=now)
    if not gate.allowed:
        return [], 0, gate
    excluded = {
        normalized_app_id(value)
        for value in state.get("excluded_apps", [])
        if isinstance(value, str) and value.strip()
    }
    allowed: list[ActivityRecord] = []
    excluded_count = 0
    for record in records:
        app_id = record_app_id(record)
        if app_id is not None and normalized_app_id(app_id) in excluded:
            excluded_count += 1
            continue
        allowed.append(
            record.model_copy(
                update={"category": normalized_category(record.category)}
            )
        )
    return allowed, excluded_count, gate
