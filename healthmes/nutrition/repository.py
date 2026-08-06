"""WellnessEvent-backed persistence for sake nutrition observations."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from re import fullmatch
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from healthmes.config import Settings
from healthmes.nutrition.contracts import (
    CaffeineConfirmation,
    ConfirmationStatus,
    DailyIntakeConfirmation,
    Estimate,
    EstimateKind,
    NutritionObservation,
    NutritionReview,
    caffeine_confirmation_from_payload,
    caffeine_confirmation_to_payload,
    confidence_score,
    daily_confirmation_from_payload,
    daily_confirmation_to_payload,
    nutrition_review_from_payload,
    nutrition_review_to_payload,
    observation_from_payload,
    observation_to_payload,
)
from healthmes.nutrition.schema import (
    CORE_NUTRIENT_UNITS,
    SUPPORTED_NUTRIENT_UNITS,
)
from healthmes.storage import classify_storage_object, ensure_default_policies
from healthmes.store import RetentionPolicy, StorageObject, WellnessEvent

OBSERVATION_EVENT = "nutrition.observation.v1"
CONFIRMATION_EVENT = "nutrition.confirmation.v1"
REVIEW_EVENT = "nutrition.review.v1"
DAILY_CONFIRMATION_EVENT = "nutrition.daily-confirmation.v1"
SOURCE_PROVIDER = "sake-vlm"


class NutritionRepositoryError(RuntimeError):
    pass


def _same_nutrition_review(
    stored: NutritionReview, incoming: NutritionReview
) -> bool:
    return (
        stored.observation_id == incoming.observation_id
        and stored.status is incoming.status
        and stored.source == incoming.source
        and stored.items == incoming.items
    )


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
            WellnessEvent.source_provider == SOURCE_PROVIDER,
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
            WellnessEvent.source_provider == SOURCE_PROVIDER,
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
        .where(
            WellnessEvent.event_type == OBSERVATION_EVENT,
            WellnessEvent.source_provider == SOURCE_PROVIDER,
        )
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


def _validate_review_estimate(estimate: Estimate) -> None:
    if not estimate.unit.strip() or len(estimate.unit) > 32:
        raise NutritionRepositoryError(
            "reviewed estimate unit must contain between 1 and 32 characters"
        )
    if (
        estimate.evidence_text is not None
        and len(estimate.evidence_text) > 500
    ):
        raise NutritionRepositoryError(
            "reviewed estimate evidence_text cannot exceed 500 characters"
        )
    if (
        estimate.estimation_basis is not None
        and len(estimate.estimation_basis) > 64
    ):
        raise NutritionRepositoryError(
            "reviewed estimate estimation_basis cannot exceed 64 characters"
        )
    values = (estimate.exact, estimate.minimum, estimate.maximum)
    if any(
        value is not None and (not isfinite(value) or value < 0)
        for value in values
    ):
        raise NutritionRepositoryError(
            "reviewed nutrition estimates must be finite and non-negative"
        )
    if estimate.kind is EstimateKind.EXACT:
        valid = (
            estimate.exact is not None
            and estimate.minimum is None
            and estimate.maximum is None
        )
    elif estimate.kind is EstimateKind.RANGE:
        valid = (
            estimate.exact is None
            and estimate.minimum is not None
            and estimate.maximum is not None
            and estimate.minimum <= estimate.maximum
        )
    else:
        valid = all(value is None for value in values)
    if not valid:
        raise NutritionRepositoryError("reviewed nutrition estimate shape is invalid")


def persist_nutrition_review(
    session: Session, review: NutritionReview
) -> WellnessEvent:
    existing = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "user-nutrition-review",
            WellnessEvent.source_record_id == str(review.review_id),
        )
    )
    if existing is not None:
        stored = nutrition_review_from_payload(existing.payload)
        if not _same_nutrition_review(stored, review):
            raise NutritionRepositoryError(
                "nutrition review operation_id was already used with different input"
            )
        return existing
    observation = get_observation(session, review.observation_id)
    if observation is None:
        raise NutritionRepositoryError("nutrition observation not found")
    if review.status not in {
        ConfirmationStatus.CONFIRMED,
        ConfirmationStatus.CORRECTED,
        ConfirmationStatus.REJECTED,
    }:
        raise NutritionRepositoryError("unsupported nutrition review status")
    if not review.source.strip() or len(review.source) > 64:
        raise NutritionRepositoryError(
            "nutrition review source must contain between 1 and 64 characters"
        )
    if review.status is ConfirmationStatus.CORRECTED:
        if len(review.items) > 50:
            raise NutritionRepositoryError(
                "at most 50 reviewed nutrition items are accepted"
            )
        expected_indexes = set(range(len(observation.items)))
        supplied_indexes = {item.item_index for item in review.items}
        if len(supplied_indexes) != len(review.items):
            raise NutritionRepositoryError(
                "nutrition review item indexes must not contain duplicates"
            )
        if supplied_indexes != expected_indexes:
            raise NutritionRepositoryError(
                "corrected nutrition review must provide every observation item"
            )
        for item in review.items:
            if not item.name.strip() or len(item.name) > 300:
                raise NutritionRepositoryError(
                    "reviewed item names must contain between 1 and 300 characters"
                )
            _validate_review_estimate(item.serving)
            if len(item.nutrients) > 100:
                raise NutritionRepositoryError(
                    "at most 100 reviewed nutrients are accepted per item"
                )
            if len(item.warnings) > 20 or any(
                len(warning) > 500 for warning in item.warnings
            ):
                raise NutritionRepositoryError(
                    "at most 20 reviewed warnings of 500 characters are accepted"
                )
            nutrients = {value.nutrient: value for value in item.nutrients}
            if len(nutrients) != len(item.nutrients):
                raise NutritionRepositoryError(
                    "reviewed nutrient names must not contain duplicates"
                )
            missing = set(CORE_NUTRIENT_UNITS) - set(nutrients)
            if missing:
                raise NutritionRepositoryError(
                    "corrected nutrition review is missing core nutrients: "
                    + ", ".join(sorted(missing))
                )
            for nutrient in item.nutrients:
                if fullmatch(
                    r"[a-z][a-z0-9_]{0,63}", nutrient.nutrient
                ) is None:
                    raise NutritionRepositoryError(
                        "reviewed nutrient names must use lowercase snake_case"
                    )
                if nutrient.amount.unit not in SUPPORTED_NUTRIENT_UNITS:
                    raise NutritionRepositoryError(
                        f"unsupported reviewed nutrient unit: {nutrient.amount.unit}"
                    )
                expected_unit = CORE_NUTRIENT_UNITS.get(nutrient.nutrient)
                if expected_unit is not None and nutrient.amount.unit != expected_unit:
                    raise NutritionRepositoryError(
                        f"reviewed {nutrient.nutrient} must use {expected_unit}"
                    )
                _validate_review_estimate(nutrient.amount)
    elif review.items:
        raise NutritionRepositoryError(
            "confirmed or rejected nutrition reviews cannot contain corrected items"
        )

    policy = _policy(session, "nutrition_observation")
    reviewed_at = _as_utc(review.reviewed_at)
    observation_at = _as_utc(observation.capture.captured_at)
    event = WellnessEvent(
        event_type=REVIEW_EVENT,
        schema_version=1,
        observed_at=observation_at,
        recorded_at=reviewed_at,
        timezone=observation.capture.timezone,
        source_provider="user-nutrition-review",
        source_device=review.source,
        source_record_id=str(review.review_id),
        capture_method="manual",
        quality_flags={"status": review.status.value},
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=_expiry(policy, observation_at),
        payload=nutrition_review_to_payload(review),
        derived_from={"observation_id": str(review.observation_id)},
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.source_provider == "user-nutrition-review",
                WellnessEvent.source_record_id == str(review.review_id),
            )
        )
        if existing is None:
            raise
        stored = nutrition_review_from_payload(existing.payload)
        if not _same_nutrition_review(stored, review):
            raise NutritionRepositoryError(
                "nutrition review operation_id was already used with different input"
            )
        return existing
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
                WellnessEvent.source_provider == SOURCE_PROVIDER,
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
        .where(
            WellnessEvent.event_type == CONFIRMATION_EVENT,
            WellnessEvent.source_provider == "user-confirmation",
        )
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


def latest_nutrition_reviews(
    session: Session, observation_ids: set[uuid.UUID]
) -> dict[uuid.UUID, NutritionReview]:
    if not observation_ids:
        return {}
    rows = session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == REVIEW_EVENT,
            WellnessEvent.source_provider == "user-nutrition-review",
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    latest: dict[uuid.UUID, NutritionReview] = {}
    for row in rows:
        review = nutrition_review_from_payload(row.payload)
        if (
            review.observation_id in observation_ids
            and review.observation_id not in latest
        ):
            latest[review.observation_id] = review
    return latest


def latest_daily_confirmation(
    session: Session, local_date: date, timezone: str
) -> DailyIntakeConfirmation | None:
    rows = session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == DAILY_CONFIRMATION_EVENT,
            WellnessEvent.source_provider == "user-confirmation",
        )
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
