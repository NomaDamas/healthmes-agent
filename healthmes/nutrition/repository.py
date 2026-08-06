"""WellnessEvent-backed persistence for sake nutrition observations."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.config import Settings
from healthmes.nutrition.contracts import (
    CaffeineConfirmation,
    ConfirmationStatus,
    DailyIntakeConfirmation,
    NutritionObservation,
    caffeine_confirmation_from_payload,
    caffeine_confirmation_to_payload,
    confidence_score,
    daily_confirmation_from_payload,
    daily_confirmation_to_payload,
    observation_from_payload,
    observation_to_payload,
)
from healthmes.storage import classify_storage_object, ensure_default_policies
from healthmes.store import RetentionPolicy, StorageObject, WellnessEvent

OBSERVATION_EVENT = "nutrition.observation.v1"
CONFIRMATION_EVENT = "nutrition.confirmation.v1"
DAILY_CONFIRMATION_EVENT = "nutrition.daily-confirmation.v1"
SOURCE_PROVIDER = "sake-vlm"


class NutritionRepositoryError(RuntimeError):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _policy(session: Session, data_class: str) -> RetentionPolicy:
    ensure_default_policies(session)
    policy = session.scalar(
        select(RetentionPolicy).where(RetentionPolicy.data_class == data_class)
    )
    if policy is None:  # pragma: no cover - ensure_default_policies owns this invariant
        raise NutritionRepositoryError(f"missing retention policy: {data_class}")
    return policy


def _expiry(policy: RetentionPolicy, observed_at: datetime) -> datetime | None:
    if not policy.enabled or policy.retention_days is None:
        return None
    return observed_at + timedelta(days=policy.retention_days)


def storage_object_for_media(session: Session, media_path: str) -> StorageObject | None:
    return session.scalar(
        select(StorageObject).where(
            StorageObject.relative_path == media_path,
            StorageObject.purged_at.is_(None),
        )
    )


def observation_for_media(
    session: Session, storage_object_id: uuid.UUID
) -> NutritionObservation | None:
    row = session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == OBSERVATION_EVENT,
            WellnessEvent.raw_object_id == storage_object_id,
        )
        .order_by(WellnessEvent.recorded_at.desc())
        .limit(1)
    )
    return observation_from_payload(row.payload) if row is not None else None


def persist_observation(
    session: Session,
    settings: Settings,
    observation: NutritionObservation,
) -> WellnessEvent:
    source_record_id = str(observation.observation_id)
    existing = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == SOURCE_PROVIDER,
            WellnessEvent.source_record_id == source_record_id,
        )
    )
    if existing is not None:
        return existing

    obj = storage_object_for_media(session, observation.capture.media_path)
    if obj is None:
        raise NutritionRepositoryError("media storage object not found")
    if not (obj.content_type or "").startswith("image/"):
        raise NutritionRepositoryError("nutrition observations require an image object")

    observed_at = _as_utc(observation.capture.captured_at)
    policy = _policy(session, "nutrition_observation")
    event = WellnessEvent(
        event_type=OBSERVATION_EVENT,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=_as_utc(observation.vision.analyzed_at),
        timezone=observation.capture.timezone,
        source_provider=SOURCE_PROVIDER,
        source_device=observation.capture.source,
        source_record_id=source_record_id,
        capture_method="photo",
        quality_flags={
            "status": observation.status.value,
            "warnings": list(observation.warnings),
        },
        confidence=confidence_score(observation.confidence),
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=_expiry(policy, observed_at),
        payload=observation_to_payload(observation),
        raw_object_id=obj.id,
        derived_from={"storage_object_id": str(obj.id)},
    )
    session.add(event)
    session.flush()
    classify_storage_object(
        session,
        obj,
        data_class="nutrition_media",
        observed_at=observed_at,
        safe_to_purge=True,
    )
    return event


def get_observation(
    session: Session, observation_id: uuid.UUID
) -> NutritionObservation | None:
    row = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == OBSERVATION_EVENT,
            WellnessEvent.source_record_id == str(observation_id),
        )
    )
    return observation_from_payload(row.payload) if row is not None else None


def list_observations(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
) -> list[NutritionObservation]:
    statement = (
        select(WellnessEvent)
        .where(WellnessEvent.event_type == OBSERVATION_EVENT)
        .order_by(WellnessEvent.observed_at.desc(), WellnessEvent.created_at.desc())
        .limit(limit)
    )
    if start is not None:
        statement = statement.where(WellnessEvent.observed_at >= _as_utc(start))
    if end is not None:
        statement = statement.where(WellnessEvent.observed_at < _as_utc(end))
    return [
        observation_from_payload(row.payload)
        for row in session.scalars(statement)
    ]


def persist_caffeine_confirmation(
    session: Session, confirmation: CaffeineConfirmation
) -> WellnessEvent:
    observation = get_observation(session, confirmation.observation_id)
    if observation is None:
        raise NutritionRepositoryError("nutrition observation not found")
    if confirmation.status not in {
        ConfirmationStatus.CONFIRMED,
        ConfirmationStatus.CORRECTED,
        ConfirmationStatus.REJECTED,
    }:
        raise NutritionRepositoryError("unsupported confirmation status")
    if confirmation.status is ConfirmationStatus.REJECTED:
        if confirmation.items:
            raise NutritionRepositoryError(
                "rejected observations cannot contain confirmed items"
            )
    else:
        expected_indexes = set(range(len(observation.items)))
        confirmed_indexes = {item.item_index for item in confirmation.items}
        if len(confirmed_indexes) != len(confirmation.items):
            raise NutritionRepositoryError(
                "confirmation item indexes must not contain duplicates"
            )
        if confirmed_indexes != expected_indexes:
            raise NutritionRepositoryError(
                "confirmation must provide caffeine_mg for every observation item"
            )
        if any(
            not isfinite(item.caffeine_mg) or item.caffeine_mg < 0
            for item in confirmation.items
        ):
            raise NutritionRepositoryError(
                "confirmed caffeine values must be finite and non-negative"
            )
    policy = _policy(session, "nutrition_confirmation")
    observed_at = _as_utc(confirmation.confirmed_at)
    event = WellnessEvent(
        event_type=CONFIRMATION_EVENT,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=observed_at,
        timezone=None,
        source_provider="user-confirmation",
        source_device=confirmation.source,
        source_record_id=str(confirmation.confirmation_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=_expiry(policy, observed_at),
        payload=caffeine_confirmation_to_payload(confirmation),
        derived_from={"observation_id": str(confirmation.observation_id)},
    )
    session.add(event)
    session.flush()
    return event


def persist_daily_confirmation(
    session: Session, confirmation: DailyIntakeConfirmation
) -> WellnessEvent:
    start, end = local_day_bounds(confirmation.local_date, confirmation.timezone)
    day_ids = {
        uuid.UUID(value)
        for value in session.scalars(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.event_type == OBSERVATION_EVENT,
                WellnessEvent.observed_at >= start,
                WellnessEvent.observed_at < end,
            )
        )
    }
    supplied_ids = set(confirmation.observation_ids)
    unknown = supplied_ids - day_ids
    if unknown:
        raise NutritionRepositoryError(
            "nutrition observations are not part of the confirmed local day: "
            + ", ".join(sorted(str(value) for value in unknown))
        )
    if confirmation.total_intake_complete and supplied_ids != day_ids:
        raise NutritionRepositoryError(
            "complete-day confirmation must include every observation for that local day"
        )
    policy = _policy(session, "nutrition_confirmation")
    observed_at = _as_utc(confirmation.confirmed_at)
    event = WellnessEvent(
        event_type=DAILY_CONFIRMATION_EVENT,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=observed_at,
        timezone=confirmation.timezone,
        source_provider="user-confirmation",
        source_device=confirmation.source,
        source_record_id=str(confirmation.confirmation_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=_expiry(policy, observed_at),
        payload=daily_confirmation_to_payload(confirmation),
        derived_from={
            "observation_ids": [str(value) for value in confirmation.observation_ids]
        },
    )
    session.add(event)
    session.flush()
    return event


def latest_caffeine_confirmations(
    session: Session, observation_ids: set[uuid.UUID]
) -> dict[uuid.UUID, CaffeineConfirmation]:
    if not observation_ids:
        return {}
    rows = session.scalars(
        select(WellnessEvent)
        .where(WellnessEvent.event_type == CONFIRMATION_EVENT)
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    latest: dict[uuid.UUID, CaffeineConfirmation] = {}
    for row in rows:
        confirmation = caffeine_confirmation_from_payload(row.payload)
        if (
            confirmation.observation_id in observation_ids
            and confirmation.observation_id not in latest
        ):
            latest[confirmation.observation_id] = confirmation
    return latest


def latest_daily_confirmation(
    session: Session, local_date: date, timezone: str
) -> DailyIntakeConfirmation | None:
    rows = session.scalars(
        select(WellnessEvent)
        .where(WellnessEvent.event_type == DAILY_CONFIRMATION_EVENT)
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    for row in rows:
        confirmation = daily_confirmation_from_payload(row.payload)
        if confirmation.local_date == local_date and confirmation.timezone == timezone:
            return confirmation
    return None


def local_day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)
