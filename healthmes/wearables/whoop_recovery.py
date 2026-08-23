"""Deterministic WHOOP recovery-package normalization for HealthMes."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from healthmes.timezones import parse_timezone

WHOOP_RECOVERY_PACKAGE_CAPABILITY = "wearable.whoop-recovery-package"
WHOOP_RECOVERY_ALGORITHM = "healthmes.whoop-recovery-package.v1"
WHOOP_RECOVERY_SNAPSHOT_DERIVER = (
    "healthmes.whoop-recovery-package.snapshot.v2"
)
WHOOP_SOURCE_PROVIDER = "open-wearables"
WHOOP_UPSTREAM_PROVIDER = "whoop"
WHOOP_RESOURCE_TYPE = "health_score"

_RECOVERY_CATEGORY = "recovery"
_DAY_STRAIN_CATEGORY = "day_strain"
_METRIC_DEFINITIONS = {
    _RECOVERY_CATEGORY: "whoop.recovery-score.0-to-100.v1",
    _DAY_STRAIN_CATEGORY: "whoop.cycle-cumulative-day-strain.0-to-21.v1",
}
_UPSTREAM_ID_FIELDS = (
    "id",
    "record_id",
    "health_score_id",
    "score_id",
)


@dataclass(frozen=True, slots=True)
class _Signal:
    category: str
    source_record_id: str | None = None
    recorded_at: datetime | None = None
    updated_at: datetime | None = None
    cycle_id: str | None = None
    raw_value: float | None = None
    label: str | None = None
    observed_on: date | None = None
    status: str = "insufficient_data"
    reason: str | None = None
    freshness: str = "unknown"
    stale_days: int | None = None
    confidence: str = "low"

    def public(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "source_category": self.category,
            "label": self.label,
            "freshness": self.freshness,
            "stale_days": self.stale_days,
            "confidence": self.confidence,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result

    def provenance(self) -> dict[str, Any] | None:
        if (
            self.source_record_id is None
            or self.recorded_at is None
            or self.updated_at is None
            or self.raw_value is None
            or not math.isfinite(self.raw_value)
        ):
            return None
        return {
            "record_id": self.source_record_id,
            "source_provider": WHOOP_SOURCE_PROVIDER,
            "upstream_provider": WHOOP_UPSTREAM_PROVIDER,
            "resource_type": WHOOP_RESOURCE_TYPE,
            "metric": self.category,
            "metric_definition": _METRIC_DEFINITIONS[self.category],
            "observed_at": self.recorded_at.isoformat(),
            "revision_at": self.updated_at.isoformat(),
            "cycle_id": self.cycle_id,
            "raw_value": self.raw_value,
            "schema_version": 1,
            "derived_by": WHOOP_RECOVERY_ALGORITHM,
        }


@dataclass(frozen=True, slots=True)
class WhoopRecoveryCalculation:
    """Public package plus private provenance for one local day."""

    public: dict[str, Any]
    provenance: tuple[dict[str, Any], ...]
    conflicting_duplicate_rows: bool = False


def calculate_whoop_recovery_package(
    recovery_rows: Sequence[Mapping[str, Any]],
    day_strain_rows: Sequence[Mapping[str, Any]],
    *,
    as_of: date,
    timezone: Any,
    recovery_truncated: bool = False,
    day_strain_truncated: bool = False,
    retained_after: datetime | None = None,
) -> WhoopRecoveryCalculation:
    """Select WHOOP rows and calculate Sake's bounded recovery package."""

    normalized_timezone = (
        parse_timezone(timezone)
        if isinstance(timezone, str)
        else timezone
    )
    retention_cutoff = (
        _aware_utc(retained_after, field="retained_after")
        if retained_after is not None
        else None
    )
    recovery, recovery_conflict = _select_signal(
        recovery_rows,
        category=_RECOVERY_CATEGORY,
        as_of=as_of,
        timezone=normalized_timezone,
        truncated=recovery_truncated,
        retained_after=retention_cutoff,
    )
    day_strain, strain_conflict = _select_signal(
        day_strain_rows,
        category=_DAY_STRAIN_CATEGORY,
        as_of=as_of,
        timezone=normalized_timezone,
        truncated=day_strain_truncated,
        retained_after=retention_cutoff,
    )
    conflicting = recovery_conflict or strain_conflict

    if recovery.status != "ok" or day_strain.status != "ok":
        cycle_linkage = {
            "status": "not_evaluated",
            "reason": "primary_signal_unavailable",
        }
    elif not recovery.cycle_id or not day_strain.cycle_id:
        cycle_linkage = {
            "status": "insufficient_data",
            "reason": "cycle_id_missing",
        }
    elif recovery.cycle_id != day_strain.cycle_id:
        cycle_linkage = {
            "status": "insufficient_data",
            "reason": "cycle_id_mismatch",
        }
    else:
        cycle_linkage = {"status": "ok", "reason": None}

    usable = (
        not conflicting
        and recovery.status == "ok"
        and day_strain.status == "ok"
        and cycle_linkage["status"] == "ok"
    )
    level = (
        _recovery_level(recovery.label, day_strain.label)
        if usable
        else None
    )
    return WhoopRecoveryCalculation(
        public={
            "status": "ok" if usable else "insufficient_data",
            "date": as_of.isoformat(),
            "timezone": str(normalized_timezone),
            "confidence": "high" if usable else "low",
            "recovery": recovery.public(),
            "day_strain": day_strain.public(),
            "cycle_linkage": cycle_linkage,
            "level": level,
            "routine_basis": (
                "whoop_primary_signals"
                if usable
                else "manual_optional"
            ),
            "actions": _package_actions(level),
            "walk": _walk_contract(level),
            "limitations": _limitations(
                recovery,
                day_strain,
                cycle_linkage=cycle_linkage,
                conflicting=conflicting,
                recovery_truncated=recovery_truncated,
                day_strain_truncated=day_strain_truncated,
            ),
        },
        provenance=tuple(
            value
            for value in (
                recovery.provenance(),
                day_strain.provenance(),
            )
            if value is not None
        ),
        conflicting_duplicate_rows=conflicting,
    )


def _select_signal(
    rows: Sequence[Mapping[str, Any]],
    *,
    category: str,
    as_of: date,
    timezone: Any,
    truncated: bool,
    retained_after: datetime | None,
) -> tuple[_Signal, bool]:
    base = _Signal(category=category)
    if truncated:
        return _replace_signal(base, reason="truncated_source"), False

    valid_recorded: list[
        tuple[Mapping[str, Any], datetime, str | None]
    ] = []
    for row in rows:
        if (
            _provider(row) != WHOOP_UPSTREAM_PROVIDER
            or _text(row.get("category")) != category
        ):
            continue
        recorded_at = _timestamp(row.get("recorded_at"))
        if recorded_at is None:
            return _replace_signal(
                base,
                reason="unparseable_recorded_at",
            ), False
        if (
            retained_after is not None
            and recorded_at <= retained_after
        ):
            continue
        valid_recorded.append(
            (row, recorded_at, _source_record_id(row))
        )

    current_day = [
        item
        for item in valid_recorded
        if item[1].astimezone(timezone).date() == as_of
    ]
    if current_day:
        selected_pool = current_day
    elif valid_recorded:
        selected_pool = valid_recorded
    else:
        return _replace_signal(
            base,
            reason=f"no_whoop_{category}",
        ), False

    candidates: list[
        tuple[Mapping[str, Any], datetime, datetime, str | None]
    ] = []
    for row, recorded_at, source_record_id in selected_pool:
        updated_at = (
            _cycle_updated_at(row)
            if category == _DAY_STRAIN_CATEGORY
            else recorded_at
        )
        if updated_at is None:
            return _replace_signal(
                base,
                reason="unparseable_cycle_updated_at",
            ), False
        candidates.append(
            (row, recorded_at, updated_at, source_record_id)
        )

    latest_updated_at = max(item[2] for item in candidates)
    latest = [
        item for item in candidates if item[2] == latest_updated_at
    ]
    if len(latest) != 1:
        return _replace_signal(
            base,
            reason="ambiguous_latest_row",
        ), True

    row, recorded_at, updated_at, source_record_id = latest[0]
    observed_on = recorded_at.astimezone(timezone).date()
    stale_days = (as_of - observed_on).days
    value = _number(row.get("value"))
    label = whoop_label(category, value)
    changes: dict[str, Any] = {
        "recorded_at": recorded_at,
        "updated_at": updated_at,
        "observed_on": observed_on,
        "freshness": (
            "current_day" if observed_on == as_of else "stale"
        ),
        "stale_days": stale_days,
        "source_record_id": source_record_id,
        "cycle_id": _cycle_id(row),
        "raw_value": value,
        "label": label,
    }
    if source_record_id is None:
        return _replace_signal(
            base,
            **changes,
            reason="source_record_id_missing",
        ), False
    if value is None:
        return _replace_signal(
            base,
            **changes,
            reason="unparseable_raw_value",
        ), False
    if label is None:
        return _replace_signal(
            base,
            **changes,
            reason="raw_value_out_of_range",
        ), False
    if observed_on != as_of:
        return _replace_signal(
            base,
            **changes,
            reason="not_current_local_day",
        ), False
    return _replace_signal(
        base,
        **changes,
        status="ok",
        confidence="high",
    ), False


def whoop_label(category: str, value: float | None) -> str | None:
    """Return Sake's exact WHOOP label boundaries."""

    if value is None or not math.isfinite(value):
        return None
    if category == _RECOVERY_CATEGORY:
        if 0 <= value <= 33:
            return "red"
        if 34 <= value <= 66:
            return "yellow"
        if 67 <= value <= 100:
            return "green"
    elif category == _DAY_STRAIN_CATEGORY:
        if 0 <= value < 10:
            return "light"
        if 10 <= value < 14:
            return "moderate"
        if 14 <= value < 18:
            return "high"
        if 18 <= value <= 21:
            return "all_out"
    return None


def _recovery_level(
    recovery_label: str | None,
    strain_label: str | None,
) -> str | None:
    if recovery_label == "red" and strain_label is not None:
        return "priority"
    if recovery_label == "green":
        return (
            "basic"
            if strain_label in {"light", "moderate"}
            else "enhanced"
            if strain_label in {"high", "all_out"}
            else None
        )
    if recovery_label == "yellow":
        return (
            "enhanced"
            if strain_label in {"light", "moderate"}
            else "priority"
            if strain_label in {"high", "all_out"}
            else None
        )
    return None


def _package_actions(level: str | None) -> list[dict[str, Any]]:
    if level == "basic":
        walks = (
            ("recommended", 10),
            ("offered", 20),
            ("offered", 30),
        )
    elif level == "enhanced":
        walks = (
            ("offered", 10),
            ("recommended", 20),
            ("offered", 30),
        )
    else:
        walks = (("offered", 10),)
    return [
        *(
            {
                "kind": "walk",
                "state": state,
                "duration_minutes": minutes,
                "advance_minutes": None,
            }
            for state, minutes in walks
        ),
        {
            "kind": "drink_water",
            "state": "recommended",
            "duration_minutes": None,
            "advance_minutes": None,
        },
        {
            "kind": "sleep_preparation",
            "state": "recommended",
            "duration_minutes": None,
            "advance_minutes": 30,
        },
    ]


def _walk_contract(level: str | None) -> dict[str, Any]:
    if level == "basic":
        return {
            "default_minutes": 10,
            "choices_minutes": [10, 20, 30],
            "rest_is_option": False,
            "pace": "comfortable_conversational",
        }
    if level == "enhanced":
        return {
            "default_minutes": 20,
            "choices_minutes": [10, 20, 30],
            "rest_is_option": False,
            "pace": "comfortable_conversational",
        }
    return {
        "default_minutes": None,
        "choices_minutes": [10],
        "rest_is_option": True,
        "pace": "comfortable_conversational",
    }


def _limitations(
    recovery: _Signal,
    day_strain: _Signal,
    *,
    cycle_linkage: Mapping[str, Any],
    conflicting: bool,
    recovery_truncated: bool,
    day_strain_truncated: bool,
) -> list[str]:
    values: list[str] = []
    if recovery_truncated:
        values.append("whoop_recovery_source_truncated")
    if day_strain_truncated:
        values.append("whoop_day_strain_source_truncated")
    if recovery_truncated or day_strain_truncated:
        values.append("wearable_upstream_page_limit_reached")
    if conflicting:
        values.append("wearable_conflicting_duplicate_rows")
    for signal in (recovery, day_strain):
        if signal.reason is not None:
            values.append(signal.reason)
    reason = cycle_linkage.get("reason")
    if isinstance(reason, str):
        values.append(reason)
    return sorted(set(values))


def _replace_signal(signal: _Signal, **changes: Any) -> _Signal:
    values = {
        field: getattr(signal, field)
        for field in signal.__dataclass_fields__
    }
    values.update(changes)
    return _Signal(**values)


def _source_record_id(row: Mapping[str, Any]) -> str | None:
    for field in _UPSTREAM_ID_FIELDS:
        value = _text(row.get(field))
        if value:
            return value
    return None


def _provider(row: Mapping[str, Any]) -> str:
    value = row.get("provider")
    if value is None and isinstance(row.get("source"), Mapping):
        value = row["source"].get("provider")
    text = _text(value)
    return text.casefold() if text else ""


def _cycle_id(row: Mapping[str, Any]) -> str | None:
    return _component_value(row, "cycle_id")


def _cycle_updated_at(row: Mapping[str, Any]) -> datetime | None:
    return _timestamp(_component_value(row, "cycle_updated_at"))


def _component_value(row: Mapping[str, Any], name: str) -> str | None:
    components = row.get("components")
    if isinstance(components, Mapping):
        value = components.get(name)
        if isinstance(value, Mapping):
            return _text(value.get("qualifier"))
        return _text(value)
    if isinstance(components, Sequence) and not isinstance(
        components,
        str | bytes,
    ):
        for component in components:
            if not isinstance(component, Mapping):
                continue
            if _text(component.get("component")) == name:
                return _text(component.get("qualifier"))
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _timestamp(value: Any) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
