"""Adapters from HealthMes domain calculations to decision context providers."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from math import isfinite
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from healthmes.activity.aggregation import local_day_bounds
from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
    recovery_activity_context,
)
from healthmes.activity.resolver import (
    _normalize_wearable_context,
    calendar_context,
)
from healthmes.calendars.base import HealthmesEventKind
from healthmes.decision.contracts import (
    ContextCoverage,
    ContextFreshness,
    ContextQuery,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    FreshnessStatus,
    PrivacyLevel,
    SourceRef,
)
from healthmes.decision.providers import (
    ContextCapability,
    ContextProviderMetadata,
    ProvenanceSupport,
)
from healthmes.nutrition.intake_contracts import CaptureModality, IntakeIntent
from healthmes.nutrition.intake_query import (
    decision_context as nutrition_decision_context,
)
from healthmes.nutrition.intake_query import search_intake_history
from healthmes.nutrition.query import known_caffeine_for_day
from healthmes.store import CalendarEventMirror, WellnessEvent
from healthmes.timezones import parse_timezone

WearableReader = Callable[[date], Awaitable[dict[str, Any]]]

_ENVELOPE_KEYS = {
    "coverage",
    "evidence_ids",
    "freshness",
    "limitations",
    "source_refs",
}
_RAW_KEYS = {
    "audio_bytes",
    "image_bytes",
    "media_path",
    "raw_bytes",
    "source_text",
}


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Enum):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_json_value(item) for item in value]
    return str(value)


def _without_raw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_raw(item)
            for key, item in value.items()
            if str(key).casefold() not in _RAW_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_without_raw(item) for item in value]
    return value


def _payload(
    raw: Mapping[str, Any],
    query: ContextQuery,
) -> dict[str, Any]:
    cleaned = {
        str(key): _json_value(value)
        for key, value in raw.items()
        if key not in _ENVELOPE_KEYS
    }
    cleaned = _without_raw(cleaned)
    if not query.fields:
        return cleaned
    return {
        key: cleaned[key]
        for key in query.fields
        if key in cleaned
    }


def _status(raw: Mapping[str, Any]) -> ContextStatus:
    value = str(raw.get("status") or "").casefold()
    if value in {"ok", "known"}:
        return ContextStatus.OK
    if value in {"partial", "insufficient_data", "incomplete"}:
        return ContextStatus.PARTIAL
    if value in {"unavailable", "not_configured"}:
        return ContextStatus.UNAVAILABLE
    if value in {"failed", "error"}:
        return ContextStatus.FAILED
    return ContextStatus.PARTIAL


def _timestamp(value: Any, *, timezone: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed_day = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime.combine(
            parsed_day,
            datetime.min.time(),
            tzinfo=parse_timezone(timezone),
        ).astimezone(UTC)
    return _as_utc(parsed) if parsed.tzinfo is not None else None


def _freshness(
    raw: Mapping[str, Any],
    *,
    now: datetime,
    timezone: str,
) -> ContextFreshness:
    value = raw.get("freshness")
    if not isinstance(value, Mapping):
        return ContextFreshness(status=FreshnessStatus.UNKNOWN)
    as_of = _timestamp(
        value.get("recorded_at") or value.get("observed_on"),
        timezone=timezone,
    )
    raw_status = str(value.get("status") or "").casefold()
    if as_of is None:
        return ContextFreshness(
            status=(
                FreshnessStatus.UNAVAILABLE
                if raw_status == "unavailable"
                else FreshnessStatus.UNKNOWN
            )
        )
    age = max(0, int((now - as_of).total_seconds()))
    return ContextFreshness(
        status=(
            FreshnessStatus.STALE
            if "stale" in raw_status
            else FreshnessStatus.CURRENT
        ),
        as_of=as_of,
        age_seconds=age,
    )


def _coverage(raw: Mapping[str, Any]) -> ContextCoverage:
    value = (
        raw.get("coverage")
        if "coverage" in raw
        else raw.get("source_coverage")
    )
    ratio: float | None = None
    if isinstance(value, int | float) and not isinstance(value, bool):
        ratio = float(value)
    elif isinstance(value, Mapping):
        candidate = value.get("ratio")
        if isinstance(candidate, int | float) and not isinstance(
            candidate, bool
        ):
            ratio = float(candidate)
        elif value.get("complete") is True:
            ratio = 1.0
    if ratio is not None and isfinite(ratio) and 0 <= ratio <= 1:
        if ratio == 1:
            return ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            )
        return ContextCoverage(
            status=CoverageStatus.PARTIAL,
            ratio=ratio,
        )
    if str(raw.get("status") or "").casefold() in {
        "unavailable",
        "failed",
    }:
        return ContextCoverage(status=CoverageStatus.UNAVAILABLE)
    return ContextCoverage(status=CoverageStatus.UNKNOWN)


def _limitations(raw: Mapping[str, Any]) -> list[str]:
    values = raw.get("limitations")
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return []
    return sorted(
        {
            str(value).strip()
            for value in values
            if str(value).strip()
        }
    )


def _event_observed_end(event: WellnessEvent) -> datetime | None:
    window = event.payload.get("window")
    if not isinstance(window, Mapping):
        return None
    end = window.get("end")
    if not isinstance(end, str):
        return None
    try:
        parsed = datetime.fromisoformat(end)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(UTC)
    return parsed if parsed > _as_utc(event.observed_at) else None


def _event_source_refs(
    session: Session,
    identifiers: Sequence[Any],
    *,
    domain: str,
    derived_by: str,
    now: datetime,
) -> tuple[list[SourceRef], bool]:
    raw_ids = [str(value) for value in identifiers if value is not None]
    if not raw_ids:
        return [], True
    uuid_ids: list[uuid.UUID] = []
    for value in raw_ids:
        try:
            uuid_ids.append(uuid.UUID(value))
        except ValueError:
            continue
    conditions = [WellnessEvent.source_record_id.in_(raw_ids)]
    if uuid_ids:
        conditions.append(WellnessEvent.id.in_(uuid_ids))
    rows = list(
        session.scalars(
            select(WellnessEvent).where(or_(*conditions))
        )
    )
    by_identity: dict[str, WellnessEvent] = {}
    for row in rows:
        by_identity[str(row.id)] = row
        by_identity[row.source_record_id] = row
    selected: list[WellnessEvent] = []
    seen: set[uuid.UUID] = set()
    for identifier in raw_ids:
        row = by_identity.get(identifier)
        if row is not None and row.id not in seen:
            seen.add(row.id)
            selected.append(row)
    refs = [
        SourceRef(
            domain=domain,
            resource_type=row.event_type,
            record_id=str(row.id),
            source_provider=row.source_provider,
            observed_start=_as_utc(row.observed_at),
            observed_end=_event_observed_end(row),
            schema_version=row.schema_version,
            derived_by=derived_by,
            freshness=(
                FreshnessStatus.STALE
                if row.expires_at is not None
                and _as_utc(row.expires_at) <= now
                else FreshnessStatus.CURRENT
            ),
            coverage=row.coverage,
            sensitivity=row.sensitivity,
        )
        for row in selected
    ]
    return refs, len(selected) == len(set(raw_ids))


def _calendar_source_refs(
    rows: Sequence[CalendarEventMirror],
) -> list[SourceRef]:
    return [
        SourceRef(
            domain="calendar",
            resource_type="calendar.event",
            record_id=str(row.id),
            source_provider="healthmes-calendar-mirror",
            observed_start=_as_utc(row.start_at),
            observed_end=_as_utc(row.end_at),
            schema_version=1,
            derived_by="calendar.context.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="calendar-metadata",
        )
        for row in rows
    ]


def _wearable_source_refs(
    raw: Mapping[str, Any],
    *,
    timezone: str,
) -> tuple[list[SourceRef], bool]:
    values = raw.get("source_refs")
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return [], False
    refs: list[SourceRef] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        observed = _timestamp(value.get("observed_at"), timezone=timezone)
        if observed is None:
            continue
        resource_type = str(value.get("resource_type") or "")
        observed_end = (
            observed + timedelta(days=1)
            if resource_type == "sleep_summary"
            else None
        )
        try:
            refs.append(
                SourceRef(
                    domain="wearable",
                    resource_type=resource_type,
                    record_id=str(value.get("record_id") or ""),
                    source_provider=str(
                        value.get("source_provider") or "open-wearables"
                    ),
                    observed_start=observed,
                    observed_end=observed_end,
                    schema_version=int(value.get("schema_version") or 1),
                    derived_by=(
                        str(value["derived_by"])
                        if value.get("derived_by")
                        else None
                    ),
                    freshness=FreshnessStatus.CURRENT,
                    sensitivity="wearable",
                )
            )
        except (TypeError, ValueError):
            continue
    return refs, len(refs) == len(values)


def _result(
    query: ContextQuery,
    raw: Mapping[str, Any],
    *,
    refs: Sequence[SourceRef],
    refs_complete: bool,
    now: datetime,
    extra_limitations: Sequence[str] = (),
    truncated: bool = False,
) -> ContextResult:
    status = _status(raw)
    limitations = {
        *_limitations(raw),
        *(value for value in extra_limitations if value),
    }
    if not refs_complete:
        limitations.add("provenance_incomplete")
    if status in {
        ContextStatus.UNAVAILABLE,
        ContextStatus.FAILED,
    }:
        payload = {}
        refs = ()
    else:
        payload = _payload(raw, query)
    return ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=status,
        payload=payload,
        source_refs=list(refs),
        freshness=_freshness(
            raw,
            now=now,
            timezone=query.timezone,
        ),
        coverage=_coverage(raw),
        limitations=sorted(limitations),
        truncated=truncated,
    )


def _query_day(
    query: ContextQuery,
    *,
    now: datetime,
) -> date:
    raw = query.parameters.get("date")
    if isinstance(raw, str):
        return date.fromisoformat(raw)
    zone = parse_timezone(query.timezone)
    if query.start is not None:
        return query.start.astimezone(zone).date()
    return now.astimezone(zone).date()


def _query_window(
    query: ContextQuery,
    *,
    now: datetime,
) -> tuple[date, datetime, datetime]:
    day = _query_day(query, now=now)
    day_start, day_end = local_day_bounds(day, query.timezone)
    start = query.start or day_start
    end = query.end or min(day_end, now + timedelta(seconds=1))
    if end <= start:
        raise ValueError("context window has not started")
    if (
        start.astimezone(parse_timezone(query.timezone)).date() != day
        or (end - timedelta(microseconds=1))
        .astimezone(parse_timezone(query.timezone))
        .date()
        != day
    ):
        raise ValueError("context query must stay within one local day")
    return day, start, end


def _validate_query(
    metadata: ContextProviderMetadata,
    query: ContextQuery,
) -> ContextCapability:
    capability = metadata.capability(query.capability)
    if capability is None:
        raise ValueError("unsupported provider capability")
    if query.granularity not in capability.granularities:
        raise ValueError("unsupported context granularity")
    if query.privacy_level not in capability.privacy_levels:
        raise ValueError("unsupported context privacy level")
    unsupported_fields = set(query.fields) - set(capability.output_fields)
    if unsupported_fields:
        raise ValueError("unsupported context output fields")
    unsupported_parameters = set(query.parameters) - set(
        capability.parameters
    )
    if unsupported_parameters:
        raise ValueError("unsupported context parameters")
    if (
        query.start is not None
        and query.end is not None
        and query.end - query.start
        > timedelta(days=capability.max_lookback_days)
    ):
        raise ValueError("context query exceeds provider lookback")
    return capability


class ActivityContextProvider:
    """Typed access to deterministic activity metrics and classifications."""

    metadata = ContextProviderMetadata(
        provider_id="activity",
        domain="activity",
        description=(
            "Deterministic device-activity summaries, focus fragmentation, "
            "overwork signals, and recovery-relevant activity metrics."
        ),
        capabilities=(
            ContextCapability(
                capability="activity.summary",
                description="One local day's aggregate activity summary.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "date",
                    "timezone",
                    "total_active_minutes",
                    "active_time_range",
                    "idle_and_break_minutes",
                    "late_activity_minutes",
                    "late_activity_time_range",
                    "longest_active_block_minutes",
                    "app_launches_or_switches",
                    "source_coverage",
                    "reason",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="activity-aggregate",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Final or provisional local-day summary.",
            ),
            ContextCapability(
                capability="activity.focus",
                description=(
                    "Focus fragmentation and sustained-block metrics for one "
                    "bounded local-day window."
                ),
                granularities=("summary", "window"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "window",
                    "classification",
                    "reason",
                    "metrics",
                    "boundary",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="activity-aggregate",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Derived from retained raw or hourly summaries.",
            ),
            ContextCapability(
                capability="activity.overwork",
                description=(
                    "Daily workload, long-block, late-activity, and personal "
                    "baseline signals without making a wellness decision."
                ),
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "date",
                    "timezone",
                    "lookback_days",
                    "risk_level",
                    "reason",
                    "signals",
                    "threshold_uncertainties",
                    "metrics",
                    "boundary",
                ),
                parameters=("date", "lookback_days"),
                max_lookback_days=90,
                sensitivity="activity-aggregate",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Current local-day summary plus trailing baseline.",
            ),
            ContextCapability(
                capability="activity.recovery",
                description="Activity-only metrics relevant to recovery context.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "date",
                    "timezone",
                    "reason",
                    "metrics",
                    "boundary",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="activity-aggregate",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="One local day's finalized or provisional activity.",
            ),
        ),
    )

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        _validate_query(self.metadata, query)
        day, start, end = _query_window(query, now=now)
        if query.capability == "activity.summary":
            raw = activity_summary_context(
                session,
                day=day,
                timezone=query.timezone,
                now=now,
            )
        elif query.capability == "activity.focus":
            raw = focus_context(
                session,
                start=start,
                end=end,
                timezone=query.timezone,
                now=now,
            )
        elif query.capability == "activity.overwork":
            lookback = query.parameters.get("lookback_days", 7)
            if isinstance(lookback, bool) or not isinstance(lookback, int):
                raise ValueError("lookback_days must be an integer")
            raw = overwork_context(
                session,
                day=day,
                timezone=query.timezone,
                lookback_days=lookback,
                now=now,
            )
        else:
            raw = recovery_activity_context(
                session,
                day=day,
                timezone=query.timezone,
                now=now,
            )
        refs, complete = _event_source_refs(
            session,
            list(raw.get("evidence_ids") or []),
            domain="activity",
            derived_by=f"{query.capability}.v1",
            now=now,
        )
        return _result(
            query,
            raw,
            refs=refs,
            refs_complete=complete,
            now=now,
        )


class NutritionContextProvider:
    """Typed reads over confirmed and candidate nutrition records."""

    metadata = ContextProviderMetadata(
        provider_id="nutrition",
        domain="nutrition",
        description=(
            "Confirmed intake history, caffeine ledgers, and structured "
            "candidate-food context from the unified wellness store."
        ),
        capabilities=(
            ContextCapability(
                capability="nutrition.intake-history",
                description=(
                    "Bounded captured intake history with optional confirmation, "
                    "nutrient, intent, modality, and text filters."
                ),
                granularities=("summary", "record"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=("status", "count", "records"),
                parameters=(
                    "confirmed_only",
                    "intent",
                    "modality",
                    "nutrient",
                    "query",
                ),
                max_lookback_days=90,
                privacy_levels=(
                    PrivacyLevel.AGGREGATE,
                    PrivacyLevel.IDENTITY,
                ),
                sensitivity="nutrition",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest retained intake state at query time.",
            ),
            ContextCapability(
                capability="nutrition.caffeine-ledger",
                description=(
                    "Confirmed local-day caffeine total and completeness state; "
                    "missing confirmation is never treated as zero."
                ),
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "local_date",
                    "timezone",
                    "confirmed_caffeine_mg",
                    "total_intake_complete",
                    "observation_count",
                    "consumed_outcome_count",
                    "outcome_state_count",
                    "ledger_entry_count",
                    "reviewed_count",
                    "unreviewed_observation_ids",
                    "unquantified_observation_ids",
                    "unquantified_outcome_ids",
                    "daily_confirmation_id",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="nutrition",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest retained daily confirmation and intake outcomes.",
            ),
            ContextCapability(
                capability="nutrition.decision-context",
                description=(
                    "Structured candidate, comparisons, confirmed history, and "
                    "specialized evidence saved for an intake decision request."
                ),
                granularities=("summary", "record"),
                query_fields=("timezone", "fields"),
                output_fields=(
                    "status",
                    "request",
                    "candidate",
                    "comparison_candidates",
                    "confirmed_intake_history",
                    "history_window",
                    "specialized_evidence",
                    "boundaries",
                ),
                parameters=("request_id",),
                max_lookback_days=90,
                privacy_levels=(
                    PrivacyLevel.AGGREGATE,
                    PrivacyLevel.IDENTITY,
                ),
                sensitivity="nutrition",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Immutable request-time context snapshot.",
            ),
        ),
    )

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        _validate_query(self.metadata, query)
        if query.capability == "nutrition.intake-history":
            intent = query.parameters.get("intent")
            modality = query.parameters.get("modality")
            raw = search_intake_history(
                session,
                start=query.start,
                end=query.end,
                intent=IntakeIntent(str(intent)) if intent is not None else None,
                modality=(
                    CaptureModality(str(modality))
                    if modality is not None
                    else None
                ),
                confirmed_only=bool(
                    query.parameters.get("confirmed_only", False)
                ),
                nutrient=(
                    str(query.parameters["nutrient"])
                    if query.parameters.get("nutrient") is not None
                    else None
                ),
                query=(
                    str(query.parameters["query"])
                    if query.parameters.get("query") is not None
                    else None
                ),
                limit=query.limit,
            )
            identifiers: list[str] = []
            for record in raw.get("records", []):
                if not isinstance(record, Mapping):
                    continue
                for key in (
                    "interaction_id",
                    "nutrition_observation_id",
                    "nutrition_review_id",
                ):
                    if record.get(key):
                        identifiers.append(str(record[key]))
                outcome = record.get("latest_outcome")
                if isinstance(outcome, Mapping) and outcome.get("outcome_id"):
                    identifiers.append(str(outcome["outcome_id"]))
                decision = record.get("latest_decision")
                if isinstance(decision, Mapping) and decision.get("decision_id"):
                    identifiers.append(str(decision["decision_id"]))
            refs, complete = _event_source_refs(
                session,
                identifiers,
                domain="nutrition",
                derived_by="nutrition.intake-history.v1",
                now=now,
            )
            raw["freshness"] = {
                "recorded_at": max(
                    (
                        str(record.get("recorded_at"))
                        for record in raw.get("records", [])
                        if isinstance(record, Mapping)
                        and record.get("recorded_at")
                    ),
                    default=None,
                ),
                "status": (
                    "stored_intake_records"
                    if raw.get("records")
                    else "unavailable"
                ),
            }
            raw["coverage"] = raw.get("coverage", {})
            return _result(
                query,
                raw,
                refs=refs,
                refs_complete=complete,
                now=now,
                truncated=bool(raw.get("truncated")),
            )

        if query.capability == "nutrition.caffeine-ledger":
            day = _query_day(query, now=now)
            raw = known_caffeine_for_day(
                session,
                local_date=day,
                timezone=query.timezone,
            )
            identifiers = [
                str(value)
                for entry in raw.get("evidence", [])
                if isinstance(entry, Mapping)
                for value in (
                    entry.get("event_id"),
                    entry.get("confirmation_id"),
                    entry.get("observation_id"),
                    entry.get("nutrition_observation_id"),
                    entry.get("nutrition_review_id"),
                )
                if value is not None
            ]
            if raw.get("daily_confirmation_id"):
                identifiers.append(str(raw["daily_confirmation_id"]))
            refs, complete = _event_source_refs(
                session,
                identifiers,
                domain="nutrition",
                derived_by="nutrition.caffeine-ledger.v1",
                now=now,
            )
            latest = max(
                (
                    ref.observed_start
                    for ref in refs
                ),
                default=None,
            )
            raw["freshness"] = {
                "recorded_at": latest.isoformat() if latest else None,
                "status": "stored_nutrition_ledger" if latest else "unavailable",
            }
            raw["coverage"] = {
                "ratio": (
                    1.0
                    if raw.get("total_intake_complete") is True
                    else None
                )
            }
            raw["limitations"] = (
                []
                if raw.get("total_intake_complete") is True
                else ["caffeine_day_not_confirmed_complete"]
            )
            return _result(
                query,
                raw,
                refs=refs,
                refs_complete=complete,
                now=now,
            )

        request_id = query.parameters.get("request_id")
        if not isinstance(request_id, str):
            raise ValueError("nutrition decision context requires request_id")
        try:
            request_uuid = uuid.UUID(request_id)
        except ValueError as exc:
            raise ValueError("request_id must be a UUID") from exc
        raw = nutrition_decision_context(
            session,
            request_id=request_uuid,
        )
        if raw is None:
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.UNAVAILABLE,
                freshness=ContextFreshness(
                    status=FreshnessStatus.UNAVAILABLE
                ),
                coverage=ContextCoverage(
                    status=CoverageStatus.UNAVAILABLE
                ),
                limitations=["nutrition_decision_request_not_found"],
            )
        refs, complete = _event_source_refs(
            session,
            list(raw.get("evidence_event_ids") or []),
            domain="nutrition",
            derived_by="nutrition.decision-context.v1",
            now=now,
        )
        raw["freshness"] = {
            "recorded_at": raw.get("request", {}).get("requested_at"),
            "status": "stored_decision_context",
        }
        raw["coverage"] = {
            "ratio": (
                1.0
                if not raw.get("history_window", {})
                .get("query", {})
                .get("truncated")
                else None
            )
        }
        return _result(
            query,
            raw,
            refs=refs,
            refs_complete=complete,
            now=now,
        )


class WearableContextProvider:
    """Normalized Open Wearables readiness data with bounded block views."""

    metadata = ContextProviderMetadata(
        provider_id="wearable",
        domain="wearable",
        description=(
            "Normalized sleep, HRV, stress, charge, and training-load context "
            "read through the Open Wearables integration."
        ),
        capabilities=(
            ContextCapability(
                capability="wearable.readiness",
                description="Full normalized daily readiness context.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "reason",
                    "date",
                    "baseline_window_days",
                    "confidence",
                    "sleep_debt",
                    "actual_sleep",
                    "hrv",
                    "stress",
                    "charge",
                    "yesterday_load",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="wearable",
                provenance=ProvenanceSupport.PARTIAL,
                freshness_expectation="Daily Open Wearables snapshot.",
            ),
            ContextCapability(
                capability="wearable.sleep",
                description="Sleep debt, actual sleep, and nocturnal HRV blocks.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "reason",
                    "date",
                    "confidence",
                    "sleep_debt",
                    "actual_sleep",
                    "hrv",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="wearable",
                provenance=ProvenanceSupport.PARTIAL,
                freshness_expectation="Latest retained or upstream sleep observations.",
            ),
            ContextCapability(
                capability="wearable.recovery",
                description="HRV, charge, and prior-day training-load blocks.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "reason",
                    "date",
                    "confidence",
                    "hrv",
                    "charge",
                    "yesterday_load",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="wearable",
                provenance=ProvenanceSupport.PARTIAL,
                freshness_expectation="Daily Open Wearables readiness snapshot.",
            ),
            ContextCapability(
                capability="wearable.stress",
                description="Normalized wearable stress block.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "reason",
                    "date",
                    "confidence",
                    "stress",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="wearable",
                provenance=ProvenanceSupport.PARTIAL,
                freshness_expectation="Latest daily stress or resilience observation.",
            ),
        ),
    )

    def __init__(self, reader: WearableReader | None = None) -> None:
        self._reader = reader

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        del session
        _validate_query(self.metadata, query)
        if self._reader is None:
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.UNAVAILABLE,
                freshness=ContextFreshness(
                    status=FreshnessStatus.UNAVAILABLE
                ),
                coverage=ContextCoverage(
                    status=CoverageStatus.UNAVAILABLE
                ),
                limitations=["open_wearables_context_unavailable"],
            )
        day = _query_day(query, now=now)
        raw = _normalize_wearable_context(
            await self._reader(day),
            day=day,
            now=now,
            timezone=parse_timezone(query.timezone),
        )
        selected = {
            "wearable.sleep": {"sleep_debt", "actual_sleep", "hrv"},
            "wearable.recovery": {"hrv", "charge", "yesterday_load"},
            "wearable.stress": {"stress"},
        }.get(query.capability)
        if selected is not None:
            raw = {
                key: value
                for key, value in raw.items()
                if key in {
                    "status",
                    "reason",
                    "date",
                    "confidence",
                    "freshness",
                    "coverage",
                    "source_refs",
                    "evidence_ids",
                    "limitations",
                    *selected,
                }
            }
        refs, complete = _wearable_source_refs(
            raw,
            timezone=query.timezone,
        )
        return _result(
            query,
            raw,
            refs=refs,
            refs_complete=complete,
            now=now,
            extra_limitations=(
                ("wearable_source_refs_are_readiness_level",)
                if selected is not None and refs
                else ()
            ),
        )


def _calendar_rows(
    session: Session,
    *,
    start: datetime,
    end: datetime,
) -> list[CalendarEventMirror]:
    return list(
        session.scalars(
            select(CalendarEventMirror)
            .where(
                CalendarEventMirror.start_at < end,
                CalendarEventMirror.end_at > start,
                CalendarEventMirror.is_all_day.is_(False),
                or_(
                    CalendarEventMirror.healthmes_kind.is_(None),
                    CalendarEventMirror.healthmes_kind
                    != HealthmesEventKind.ACTUAL_SLEEP.value,
                ),
            )
            .order_by(CalendarEventMirror.start_at)
        )
    )


def _merged_spans(
    rows: Sequence[CalendarEventMirror],
    *,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    spans = sorted(
        (
            max(_as_utc(row.start_at), start),
            min(_as_utc(row.end_at), end),
        )
        for row in rows
        if min(_as_utc(row.end_at), end)
        > max(_as_utc(row.start_at), start)
    )
    merged: list[tuple[datetime, datetime]] = []
    for span_start, span_end in spans:
        if not merged or span_start > merged[-1][1]:
            merged.append((span_start, span_end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
    return merged


class CalendarContextProvider:
    """Privacy-minimized calendar workload and availability context."""

    metadata = ContextProviderMetadata(
        provider_id="calendar",
        domain="calendar",
        description=(
            "Mirrored calendar occupancy, meeting density, busy intervals, "
            "and available windows without event titles."
        ),
        capabilities=(
            ContextCapability(
                capability="calendar.day-summary",
                description="One local day's event count and occupied minutes.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "date",
                    "timezone",
                    "event_count",
                    "busy_minutes",
                    "first_event_at",
                    "last_event_at",
                    "meeting_density_per_hour",
                ),
                parameters=("date",),
                max_lookback_days=1,
                sensitivity="calendar-metadata",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest local calendar mirror state.",
            ),
            ContextCapability(
                capability="calendar.busy-intervals",
                description="Merged occupied intervals in a bounded window.",
                granularities=("summary", "window"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=(
                    "status",
                    "window",
                    "event_count",
                    "busy_minutes",
                    "intervals",
                ),
                parameters=("date",),
                max_lookback_days=31,
                sensitivity="calendar-metadata",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest local calendar mirror state.",
            ),
            ContextCapability(
                capability="calendar.available-windows",
                description="Unoccupied intervals in a bounded window.",
                granularities=("summary", "window"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=(
                    "status",
                    "window",
                    "available_minutes",
                    "windows",
                ),
                parameters=("date", "minimum_minutes"),
                max_lookback_days=31,
                sensitivity="calendar-metadata",
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest local calendar mirror state.",
            ),
        ),
    )

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        _validate_query(self.metadata, query)
        day = _query_day(query, now=now)
        day_start, day_end = local_day_bounds(day, query.timezone)
        start = query.start or day_start
        end = query.end or day_end
        rows = _calendar_rows(session, start=start, end=end)
        refs = _calendar_source_refs(rows)
        freshness = max(
            (_as_utc(row.updated_at) for row in rows),
            default=None,
        )
        if query.capability == "calendar.day-summary":
            raw = calendar_context(
                session,
                day=day,
                timezone=query.timezone,
            )
            hours = max(
                1 / 60,
                (end - start).total_seconds() / 3600,
            )
            raw["meeting_density_per_hour"] = round(
                len(rows) / hours,
                3,
            )
        else:
            spans = _merged_spans(rows, start=start, end=end)
            if query.capability == "calendar.busy-intervals":
                selected = spans[: query.limit]
                raw = {
                    "status": "ok" if rows else "partial",
                    "window": {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "timezone": query.timezone,
                    },
                    "event_count": len(rows),
                    "busy_minutes": round(
                        sum(
                            (span_end - span_start).total_seconds()
                            for span_start, span_end in spans
                        )
                        / 60,
                        2,
                    ),
                    "intervals": [
                        {
                            "start": span_start.isoformat(),
                            "end": span_end.isoformat(),
                        }
                        for span_start, span_end in selected
                    ],
                    "truncated": len(spans) > query.limit,
                }
            else:
                minimum = query.parameters.get("minimum_minutes", 15)
                if isinstance(minimum, bool) or not isinstance(
                    minimum, int | float
                ):
                    raise ValueError("minimum_minutes must be numeric")
                cursor = start
                available: list[tuple[datetime, datetime]] = []
                for span_start, span_end in spans:
                    if (
                        span_start - cursor
                    ).total_seconds() >= float(minimum) * 60:
                        available.append((cursor, span_start))
                    cursor = max(cursor, span_end)
                if (end - cursor).total_seconds() >= float(minimum) * 60:
                    available.append((cursor, end))
                selected = available[: query.limit]
                raw = {
                    "status": "ok" if rows else "partial",
                    "window": {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "timezone": query.timezone,
                    },
                    "available_minutes": round(
                        sum(
                            (window_end - window_start).total_seconds()
                            for window_start, window_end in available
                        )
                        / 60,
                        2,
                    ),
                    "windows": [
                        {
                            "start": window_start.isoformat(),
                            "end": window_end.isoformat(),
                        }
                        for window_start, window_end in selected
                    ],
                    "truncated": len(available) > query.limit,
                }
        raw["freshness"] = {
            "recorded_at": freshness.isoformat() if freshness else None,
            "status": "calendar_mirror" if freshness else "unavailable",
        }
        raw["coverage"] = {
            "status": "calendar_mirror_rows" if rows else "no_data",
            "ratio": None,
        }
        raw["limitations"] = sorted(
            {
                *list(raw.get("limitations") or []),
                "calendar_titles_omitted",
                "calendar_mirror_completeness_unknown",
            }
        )
        return _result(
            query,
            raw,
            refs=refs,
            refs_complete=True,
            now=now,
            truncated=bool(raw.get("truncated")),
        )
