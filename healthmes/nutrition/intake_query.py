"""Read models for device clients and nutrition-aware decision agents."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from heapq import heappush, heapreplace
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes import clock
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    IntakeDecision,
    IntakeDecisionRequest,
    IntakeIntent,
    IntakeInteraction,
    IntakeOutcome,
    IntakeOutcomeStatus,
    NormalizedIntakeItem,
    StructuredIntakeSnapshot,
    decision_request_from_payload,
    interaction_from_payload,
    interaction_to_payload,
    outcome_from_payload,
    outcome_to_payload,
)
from healthmes.nutrition.intake_service import (
    DECISION_REQUEST_EVENT,
    INTERACTION_EVENT,
    OUTCOME_EVENT,
    get_decision_request,
    get_interaction,
    latest_decision,
    latest_outcome,
    resolved_items,
    structured_snapshot,
)
from healthmes.nutrition.query import known_caffeine_for_day
from healthmes.store import WellnessEvent
from healthmes.timezones import parse_timezone

_ITEMS_ADAPTER = TypeAdapter(tuple[NormalizedIntakeItem, ...])
_SNAPSHOT_ADAPTER = TypeAdapter(StructuredIntakeSnapshot)
_SOURCE_EVENT_IDS_KEY = "_source_event_ids"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _item_payload(items: tuple[Any, ...]) -> list[dict[str, Any]]:
    return _ITEMS_ADAPTER.dump_python(items, mode="json")


def _decision_view(decision: IntakeDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "decision_id": str(decision.decision_id),
        "request_id": str(decision.request_id),
        "scope": decision.scope.value,
        "status": decision.status.value,
        "decided_at": decision.decided_at.isoformat(),
        "summary": decision.summary,
        "limitations": list(decision.limitations),
        "recommendation": decision.recommendation,
    }


def _outcome_view(outcome: IntakeOutcome | None) -> dict[str, Any] | None:
    if outcome is None:
        return None
    return {
        "outcome_id": str(outcome.outcome_id),
        "interaction_id": str(outcome.interaction_id),
        "status": outcome.status.value,
        "confirmed_at": outcome.confirmed_at.isoformat(),
        "consumed_at": (
            outcome.consumed_at.isoformat()
            if outcome.consumed_at is not None
            else None
        ),
    }


def _structured_view(
    snapshot: StructuredIntakeSnapshot,
    outcome: IntakeOutcome | None,
    decision: IntakeDecision | None,
) -> dict[str, Any]:
    payload = _SNAPSHOT_ADAPTER.dump_python(snapshot, mode="json")
    payload.update(
        {
            "recorded_at": None,
            "source_text": None,
            "media_path": None,
            "latest_outcome": _outcome_view(outcome),
            "latest_decision": _decision_view(decision),
            "resolved_items": _item_payload(snapshot.items),
            "is_confirmed_intake": bool(
                outcome is not None
                and outcome.status is IntakeOutcomeStatus.CONSUMED
            ),
            "raw_capture_available": False,
        }
    )
    return payload


def _interaction_view(
    interaction: IntakeInteraction,
    outcome: IntakeOutcome | None,
    decision: IntakeDecision | None,
) -> dict[str, Any]:
    payload = interaction_to_payload(interaction)
    payload["latest_outcome"] = (
        outcome_to_payload(outcome) if outcome is not None else None
    )
    payload["latest_decision"] = _decision_view(decision)
    payload["resolved_items"] = _item_payload(resolved_items(interaction, outcome))
    payload["is_confirmed_intake"] = bool(
        outcome is not None and outcome.status is IntakeOutcomeStatus.CONSUMED
    )
    payload["raw_capture_available"] = bool(
        interaction.source_text or interaction.media_path
    )
    return payload


def _with_source_event_ids(
    record: dict[str, Any],
    event_ids: list[uuid.UUID],
) -> dict[str, Any]:
    record[_SOURCE_EVENT_IDS_KEY] = list(
        dict.fromkeys(str(value) for value in event_ids)
    )
    return record


def interaction_view(
    session: Session, interaction_id: uuid.UUID
) -> dict[str, Any] | None:
    interaction = get_interaction(session, interaction_id)
    outcome_entry = latest_outcome(session, interaction_id)
    decision_entry = latest_decision(session, interaction_id)
    outcome = outcome_entry[1] if outcome_entry is not None else None
    decision = decision_entry[1] if decision_entry is not None else None
    if interaction is not None:
        return _interaction_view(interaction, outcome, decision)
    if outcome is not None and outcome.intake_snapshot is not None:
        return _structured_view(outcome.intake_snapshot, outcome, decision)
    request_candidate = _latest_request_candidate(session, interaction_id)
    if request_candidate is not None:
        request_candidate["latest_decision"] = _decision_view(decision)
        return request_candidate
    return None


def _latest_request_candidate(
    session: Session,
    interaction_id: uuid.UUID,
) -> dict[str, Any] | None:
    rows = session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == DECISION_REQUEST_EVENT,
            (
                WellnessEvent.expires_at.is_(None)
                | (WellnessEvent.expires_at > clock.utc_now())
            ),
            WellnessEvent.payload["interaction_id"].as_string()
            == str(interaction_id),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    for row in rows:
        request = decision_request_from_payload(row.payload)
        if request.context_snapshot is not None:
            return dict(request.context_snapshot["candidate"])
    return None


def _matches_record(
    record: dict[str, Any],
    *,
    confirmed_only: bool,
    nutrient_key: str | None,
    needle: str | None,
) -> bool:
    if confirmed_only and not record["is_confirmed_intake"]:
        return False
    items = record["resolved_items"]
    if nutrient_key and not any(
        fact["nutrient"].casefold() == nutrient_key
        for item in items
        for fact in item["nutrients"]
    ):
        return False
    if needle:
        haystack = " ".join(
            [
                record.get("source_text") or "",
                *(item["name"] for item in items),
                *(
                    fact.get("evidence_text") or ""
                    for item in items
                    for fact in item["nutrients"]
                ),
            ]
        ).casefold()
        if needle not in haystack:
            return False
    return True


def search_intake_history(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    intent: IntakeIntent | None = None,
    modality: CaptureModality | None = None,
    confirmed_only: bool = False,
    nutrient: str | None = None,
    query: str | None = None,
    limit: int = 100,
    max_scan_records: int | None = None,
    include_source_event_ids: bool = False,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    if max_scan_records is None:
        max_scan_records = max(1_000, limit * 20)
    if max_scan_records < limit:
        raise ValueError("max_scan_records must be at least limit")
    needle = query.strip().casefold() if query else None
    nutrient_key = nutrient.strip().casefold() if nutrient else None
    selected: list[tuple[float, int, dict[str, Any]]] = []
    represented_ids: set[uuid.UUID] = set()
    matching_records = 0
    scanned_records = 0
    scanned_event_rows = 0
    remaining_scan_records = max_scan_records
    scan_truncated = False
    sequence = 0
    now = clock.utc_now()
    start_utc = _as_utc(start) if start is not None else None
    end_utc = _as_utc(end) if end is not None else None

    def bounded_rows(statement) -> list[WellnessEvent]:
        nonlocal remaining_scan_records, scan_truncated, scanned_event_rows
        if remaining_scan_records <= 0:
            scan_truncated = True
            return []
        rows = list(
            session.scalars(
                statement.limit(remaining_scan_records + 1)
            )
        )
        if len(rows) > remaining_scan_records:
            scan_truncated = True
            rows = rows[:remaining_scan_records]
        remaining_scan_records -= len(rows)
        scanned_event_rows += len(rows)
        return rows

    def add_time_bounds(statement):
        if start_utc is not None:
            statement = statement.where(
                WellnessEvent.observed_at >= start_utc
            )
        if end_utc is not None:
            statement = statement.where(
                WellnessEvent.observed_at < end_utc
            )
        return statement

    def include_record(record: dict[str, Any]) -> None:
        nonlocal matching_records, sequence
        observed_at = _as_utc(datetime.fromisoformat(record["observed_at"]))
        if start is not None and observed_at < _as_utc(start):
            return
        if end is not None and observed_at >= _as_utc(end):
            return
        if intent is not None and record["intent"] != intent.value:
            return
        if modality is not None and record["modality"] != modality.value:
            return
        if not _matches_record(
            record,
            confirmed_only=confirmed_only,
            nutrient_key=nutrient_key,
            needle=needle,
        ):
            return
        matching_records += 1
        sequence += 1
        entry = (observed_at.timestamp(), sequence, record)
        if len(selected) < limit + 1:
            heappush(selected, entry)
        elif entry[:2] > selected[0][:2]:
            heapreplace(selected, entry)

    interaction_statement = (
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == INTERACTION_EVENT,
            (
                WellnessEvent.expires_at.is_(None)
                | (WellnessEvent.expires_at > now)
            ),
        )
        .order_by(WellnessEvent.observed_at.desc(), WellnessEvent.created_at.desc())
        .execution_options(yield_per=200)
    )
    for event in bounded_rows(add_time_bounds(interaction_statement)):
        persisted = interaction_from_payload(event.payload)
        interaction = get_interaction(session, persisted.interaction_id)
        if interaction is None:
            continue
        represented_ids.add(interaction.interaction_id)
        scanned_records += 1
        outcome_entry = latest_outcome(session, interaction.interaction_id)
        decision_entry = latest_decision(session, interaction.interaction_id)
        record = _interaction_view(
            interaction,
            outcome_entry[1] if outcome_entry is not None else None,
            decision_entry[1] if decision_entry is not None else None,
        )
        source_event_ids = [event.id]
        if outcome_entry is not None:
            source_event_ids.append(outcome_entry[0].id)
        if decision_entry is not None:
            source_event_ids.append(decision_entry[0].id)
        source_event_ids.extend(
            _nutrition_source_event_ids(session, record)
        )
        include_record(
            _with_source_event_ids(record, source_event_ids)
        )

    seen_outcomes: set[uuid.UUID] = set()
    outcome_statement = (
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == OUTCOME_EVENT,
            (
                WellnessEvent.expires_at.is_(None)
                | (WellnessEvent.expires_at > now)
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
        .execution_options(yield_per=200)
    )
    # Outcome event time is confirmation/consumption time, while the fallback
    # record is anchored to its retained intake snapshot's observation time.
    for event in bounded_rows(outcome_statement):
        outcome = outcome_from_payload(event.payload)
        interaction_id = outcome.interaction_id
        if interaction_id in seen_outcomes:
            continue
        seen_outcomes.add(interaction_id)
        if interaction_id in represented_ids or outcome.intake_snapshot is None:
            continue
        represented_ids.add(interaction_id)
        scanned_records += 1
        decision_entry = latest_decision(session, interaction_id)
        record = _structured_view(
            outcome.intake_snapshot,
            outcome,
            decision_entry[1] if decision_entry is not None else None,
        )
        source_event_ids = [event.id]
        if decision_entry is not None:
            source_event_ids.append(decision_entry[0].id)
        source_event_ids.extend(
            _nutrition_source_event_ids(session, record)
        )
        include_record(
            _with_source_event_ids(record, source_event_ids)
        )

    seen_requests: set[uuid.UUID] = set()
    request_statement = (
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == DECISION_REQUEST_EVENT,
            (
                WellnessEvent.expires_at.is_(None)
                | (WellnessEvent.expires_at > now)
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
        .execution_options(yield_per=200)
    )
    # Request event time is when advice was requested, not when its retained
    # candidate was observed. include_record applies the authoritative bound.
    for event in bounded_rows(request_statement):
        request = decision_request_from_payload(event.payload)
        interaction_id = request.interaction_id
        if interaction_id in seen_requests:
            continue
        seen_requests.add(interaction_id)
        if (
            interaction_id in represented_ids
            or request.context_snapshot is None
        ):
            continue
        represented_ids.add(interaction_id)
        scanned_records += 1
        record = dict(request.context_snapshot["candidate"])
        decision_entry = latest_decision(session, interaction_id)
        record["latest_decision"] = _decision_view(
            decision_entry[1] if decision_entry is not None else None
        )
        source_event_ids = [event.id]
        if decision_entry is not None:
            source_event_ids.append(decision_entry[0].id)
        source_event_ids.extend(
            _nutrition_source_event_ids(session, record)
        )
        include_record(
            _with_source_event_ids(record, source_event_ids)
        )

    ordered = sorted(selected, key=lambda value: value[:2], reverse=True)
    records = [record for _timestamp, _sequence, record in ordered[:limit]]
    source_event_ids: list[str] = []
    for record in records:
        source_event_ids.extend(record.pop(_SOURCE_EVENT_IDS_KEY, []))
    truncated = matching_records > limit or scan_truncated
    coverage: dict[str, Any] = {
        "complete": not truncated,
        "scanned_records": scanned_records,
        "matching_records": matching_records,
        "result_limit": limit,
    }
    limitations: list[str] = []
    if scan_truncated:
        coverage.update(
            {
                "scan_limit": max_scan_records,
                "scanned_event_rows": scanned_event_rows,
            }
        )
        limitations.append("nutrition_history_scan_limit_reached")
    result = {
        "status": "ok",
        "count": len(records),
        "records": records,
        "truncated": truncated,
        "coverage": coverage,
        "limitations": limitations,
    }
    if include_source_event_ids:
        result["source_event_ids"] = list(
            dict.fromkeys(source_event_ids)
        )
    return result


def _confirmed_history(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    exclude_interaction_ids: set[uuid.UUID] | None = None,
    limit: int = 500,
) -> tuple[list[dict[str, Any]], list[uuid.UUID], dict[str, Any]]:
    excluded = exclude_interaction_ids or set()
    selected: list[tuple[float, int, dict[str, Any], uuid.UUID]] = []
    seen: set[uuid.UUID] = set()
    matching_records = 0
    scanned_latest_outcomes = 0
    sequence = 0
    statement = (
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == OUTCOME_EVENT,
            (
                WellnessEvent.expires_at.is_(None)
                | (WellnessEvent.expires_at > clock.utc_now())
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
        .execution_options(yield_per=200)
    )
    for outcome_event in session.scalars(statement):
        outcome = outcome_from_payload(outcome_event.payload)
        interaction_id = outcome.interaction_id
        if interaction_id in seen:
            continue
        seen.add(interaction_id)
        scanned_latest_outcomes += 1
        if interaction_id in excluded:
            continue
        if outcome.status is not IntakeOutcomeStatus.CONSUMED:
            continue
        if outcome.consumed_at is None or outcome.intake_snapshot is None:
            continue
        consumed_utc = _as_utc(outcome.consumed_at)
        if not (_as_utc(start) <= consumed_utc < _as_utc(end)):
            continue
        matching_records += 1
        sequence += 1
        decision_entry = latest_decision(session, interaction_id)
        entry = (
            consumed_utc.timestamp(),
            sequence,
            _structured_view(
                outcome.intake_snapshot,
                outcome,
                decision_entry[1] if decision_entry is not None else None,
            ),
            outcome_event.id,
        )
        if len(selected) < limit + 1:
            heappush(selected, entry)
        elif entry[:2] > selected[0][:2]:
            heapreplace(selected, entry)
    ordered = sorted(selected, key=lambda value: value[:2], reverse=True)
    results = ordered[:limit]
    coverage = {
        "complete": matching_records <= limit,
        "truncated": matching_records > limit,
        "scanned_latest_outcomes": scanned_latest_outcomes,
        "matching_records": matching_records,
        "result_limit": limit,
    }
    return (
        [record for _timestamp, _sequence, record, _event_id in results],
        [event_id for _timestamp, _sequence, _record, event_id in results],
        coverage,
    )


def _interaction_event_id(
    session: Session, interaction_id: uuid.UUID
) -> uuid.UUID | None:
    return session.scalar(
        select(WellnessEvent.id).where(
            WellnessEvent.event_type == INTERACTION_EVENT,
            WellnessEvent.source_record_id == str(interaction_id),
        )
    )


def _snapshot_for_interaction(
    session: Session,
    interaction_id: uuid.UUID,
) -> tuple[dict[str, Any], list[uuid.UUID]]:
    interaction = get_interaction(session, interaction_id)
    if interaction is None:
        raise RuntimeError("decision interaction disappeared before snapshot")
    outcome_entry = latest_outcome(session, interaction_id)
    decision_entry = latest_decision(session, interaction_id)
    outcome = outcome_entry[1] if outcome_entry is not None else None
    decision = decision_entry[1] if decision_entry is not None else None
    evidence_ids = []
    interaction_event_id = _interaction_event_id(session, interaction_id)
    if interaction_event_id is not None:
        evidence_ids.append(interaction_event_id)
    if outcome_entry is not None:
        evidence_ids.append(outcome_entry[0].id)
    if decision_entry is not None:
        evidence_ids.append(decision_entry[0].id)
    return (
        _structured_view(structured_snapshot(interaction), outcome, decision),
        evidence_ids,
    )


def _nutrition_source_event_ids(
    session: Session,
    record: dict[str, Any],
) -> list[uuid.UUID]:
    event_ids: list[uuid.UUID] = []
    observation_id = record.get("nutrition_observation_id")
    if observation_id:
        event_id = session.scalar(
            select(WellnessEvent.id).where(
                WellnessEvent.event_type == "nutrition.observation.v1",
                WellnessEvent.source_provider == "sake-vlm",
                WellnessEvent.source_record_id == observation_id,
            )
        )
        if event_id is not None:
            event_ids.append(event_id)
    review_id = record.get("nutrition_review_id")
    if review_id:
        event_id = session.scalar(
            select(WellnessEvent.id).where(
                WellnessEvent.event_type == "nutrition.review.v1",
                WellnessEvent.source_provider == "user-nutrition-review",
                WellnessEvent.source_record_id == review_id,
            )
        )
        if event_id is not None:
            event_ids.append(event_id)
    return event_ids


def _caffeine_gate(
    session: Session,
    *,
    request: IntakeDecisionRequest,
    anchor: datetime,
    timezone: str,
) -> tuple[dict[str, Any] | None, list[uuid.UUID]]:
    if request.scope.value != "caffeine_sleep":
        return None, []
    try:
        local_timezone = parse_timezone(timezone)
    except ValueError:
        return (
            {
                "status": "unavailable",
                "local_date": None,
                "timezone": timezone,
                "confirmed_caffeine_mg": 0.0,
                "total_intake_complete": False,
                "observation_count": 0,
                "reviewed_count": 0,
                "unreviewed_observation_ids": [],
                "unquantified_observation_ids": [],
                "evidence": [],
                "daily_confirmation_id": None,
                "reason": "interaction timezone is invalid",
            },
            [],
        )
    local_date = anchor.astimezone(local_timezone).date()
    gate = known_caffeine_for_day(
        session,
        local_date=local_date,
        timezone=timezone,
    )
    observation_ids = {
        entry.get("observation_id") or entry.get("nutrition_observation_id")
        for entry in gate["evidence"]
        if isinstance(
            entry.get("observation_id")
            or entry.get("nutrition_observation_id"),
            str,
        )
    }
    confirmation_ids = {
        entry["confirmation_id"]
        for entry in gate["evidence"]
        if entry.get("event_type") == "nutrition.confirmation.v1"
    }
    review_ids = {
        entry.get("confirmation_id") or entry.get("nutrition_review_id")
        for entry in gate["evidence"]
        if (
            entry.get("event_type") == "nutrition.review.v1"
            or isinstance(entry.get("nutrition_review_id"), str)
        )
    }
    event_ids: list[uuid.UUID] = []
    for entry in gate["evidence"]:
        if entry.get("event_type") != "nutrition.intake-outcome.v1":
            continue
        try:
            event_ids.append(uuid.UUID(str(entry["event_id"])))
        except (KeyError, TypeError, ValueError):
            continue
    if observation_ids:
        event_ids.extend(
            session.scalars(
                select(WellnessEvent.id).where(
                    WellnessEvent.event_type == "nutrition.observation.v1",
                    WellnessEvent.source_provider == "sake-vlm",
                    WellnessEvent.source_record_id.in_(observation_ids),
                )
            )
        )
    if confirmation_ids:
        event_ids.extend(
            session.scalars(
                select(WellnessEvent.id).where(
                    WellnessEvent.event_type == "nutrition.confirmation.v1",
                    WellnessEvent.source_provider == "user-confirmation",
                    WellnessEvent.source_record_id.in_(confirmation_ids),
                )
            )
        )
    if review_ids:
        event_ids.extend(
            session.scalars(
                select(WellnessEvent.id).where(
                    WellnessEvent.event_type == "nutrition.review.v1",
                    WellnessEvent.source_provider == "user-nutrition-review",
                    WellnessEvent.source_record_id.in_(review_ids),
                )
            )
        )
    if gate["daily_confirmation_id"] is not None:
        daily_event_id = session.scalar(
            select(WellnessEvent.id).where(
                WellnessEvent.event_type
                == "nutrition.daily-confirmation.v1",
                WellnessEvent.source_provider == "user-confirmation",
                WellnessEvent.source_record_id
                == gate["daily_confirmation_id"],
            )
        )
        if daily_event_id is not None:
            event_ids.append(daily_event_id)
    return gate, event_ids


def build_decision_context_snapshot(
    session: Session,
    *,
    request: IntakeDecisionRequest,
    request_event_id: uuid.UUID,
) -> dict[str, Any]:
    primary, primary_event_ids = _snapshot_for_interaction(
        session, request.interaction_id
    )
    primary_event_ids.extend(
        _nutrition_source_event_ids(session, primary)
    )
    comparisons: list[dict[str, Any]] = []
    comparison_event_ids: list[uuid.UUID] = []
    for interaction_id in request.compare_interaction_ids:
        value, event_ids = _snapshot_for_interaction(session, interaction_id)
        comparisons.append(value)
        comparison_event_ids.extend(event_ids)
        comparison_event_ids.extend(
            _nutrition_source_event_ids(session, value)
        )

    anchor = request.intended_consumption_at or request.requested_at
    history_start = anchor - timedelta(days=request.lookback_days)
    history, history_event_ids, history_coverage = _confirmed_history(
        session,
        start=history_start,
        end=anchor + timedelta(microseconds=1),
        exclude_interaction_ids={
            request.interaction_id,
            *request.compare_interaction_ids,
        },
    )
    caffeine_gate, caffeine_event_ids = _caffeine_gate(
        session,
        request=request,
        anchor=anchor,
        timezone=primary["timezone"],
    )
    evidence_ids = [
        request_event_id,
        *primary_event_ids,
        *comparison_event_ids,
        *history_event_ids,
        *caffeine_event_ids,
    ]
    return {
        "status": "ok",
        "request": {
            "request_id": str(request.request_id),
            "scope": request.scope.value,
            "question": request.question,
            "requested_at": request.requested_at.isoformat(),
            "intended_consumption_at": (
                request.intended_consumption_at.isoformat()
                if request.intended_consumption_at is not None
                else None
            ),
        },
        "candidate": primary,
        "comparison_candidates": comparisons,
        "confirmed_intake_history": history,
        "history_window": {
            "start": history_start.isoformat(),
            "end": anchor.isoformat(),
            "lookback_days": request.lookback_days,
            "coverage": "captured_records_only",
            "query": history_coverage,
        },
        "specialized_evidence": {"caffeine": caffeine_gate},
        "evidence_event_ids": list(
            dict.fromkeys(str(value) for value in evidence_ids)
        ),
        "boundaries": {
            "candidate_is_not_consumed": not primary["is_confirmed_intake"],
            "history_is_not_complete_day_proof": True,
            "medical_safety_requires_separate_policy": True,
            "generic_caffeine_actionable_decisions_forbidden": (
                request.scope.value == "caffeine_sleep"
            ),
            "caffeine_total_intake_complete": (
                caffeine_gate["total_intake_complete"]
                if caffeine_gate is not None
                else None
            ),
        },
    }


def decision_context(
    session: Session,
    *,
    request_id: uuid.UUID,
) -> dict[str, Any] | None:
    request_entry = get_decision_request(session, request_id)
    if request_entry is None:
        return None
    request = request_entry[1]
    return request.context_snapshot
