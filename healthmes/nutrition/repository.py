"""WellnessEvent-backed persistence for sake nutrition observations."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from re import fullmatch

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from healthmes import clock
from healthmes.activity.locking import lock_activity_write_plane
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
from healthmes.nutrition.intake_contracts import (
    IntakeOutcome,
    IntakeOutcomeStatus,
    outcome_from_payload,
)
from healthmes.nutrition.schema import (
    CORE_NUTRIENT_UNITS,
    SUPPORTED_NUTRIENT_UNITS,
)
from healthmes.storage import classify_storage_object, ensure_default_policies
from healthmes.store import RetentionPolicy, StorageObject, WellnessEvent
from healthmes.timezones import parse_timezone

OBSERVATION_EVENT = "nutrition.observation.v1"
CONFIRMATION_EVENT = "nutrition.confirmation.v1"
REVIEW_EVENT = "nutrition.review.v1"
DAILY_CONFIRMATION_EVENT = "nutrition.daily-confirmation.v1"
SOURCE_PROVIDER = "sake-vlm"
RAW_OBSERVATION_EVENT = "nutrition.observation-raw.v1"
RAW_SOURCE_PROVIDER = "sake-vlm-raw"
INTAKE_OUTCOME_EVENT = "nutrition.intake-outcome.v1"
INTAKE_OUTCOME_PROVIDER = "nutrition-intake-outcome"
MAX_CAPTURE_CLOCK_SKEW = timedelta(minutes=5)


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


def _event_is_expired(event: WellnessEvent) -> bool:
    return (
        event.expires_at is not None
        and _as_utc(event.expires_at) <= clock.utc_now()
    )


def _validate_observation_retry(
    event: WellnessEvent,
    *,
    media_object_id: uuid.UUID | None,
    request_fingerprint: str,
) -> None:
    if _event_is_expired(event):
        raise NutritionRepositoryError(
            "expired nutrition observation cannot be retried"
        )
    derived_from = event.derived_from
    if (
        not isinstance(derived_from, dict)
        or derived_from.get("request_fingerprint")
        != request_fingerprint
    ):
        raise NutritionRepositoryError(
            "nutrition observation_id was already used with different request input"
        )
    stored_media_id = derived_from.get("storage_object_id")
    if (
        media_object_id is None
        or event.raw_object_id != media_object_id
        or (
            stored_media_id is not None
            and stored_media_id != str(media_object_id)
        )
    ):
        raise NutritionRepositoryError(
            "nutrition observation_id was already used with different media"
        )


def _policy(session: Session, data_class: str) -> RetentionPolicy:
    policy = session.scalar(
        select(RetentionPolicy)
        .where(RetentionPolicy.data_class == data_class)
        .execution_options(populate_existing=True)
    )
    if policy is None:  # pragma: no cover - ensure_default_policies owns this invariant
        raise NutritionRepositoryError(f"missing retention policy: {data_class}")
    return policy


def _expiry(policy: RetentionPolicy, observed_at: datetime) -> datetime | None:
    if not policy.enabled or policy.retention_days is None:
        return None
    return observed_at + timedelta(days=policy.retention_days)


def storage_object_for_media(session: Session, media_path: str) -> StorageObject | None:
    now = clock.utc_now()
    return session.scalar(
        select(StorageObject).where(
            StorageObject.relative_path == media_path,
            StorageObject.purged_at.is_(None),
            or_(
                StorageObject.expires_at.is_(None),
                StorageObject.expires_at > now,
            ),
        )
    )


def observation_for_media(
    session: Session,
    storage_object_id: uuid.UUID,
    *,
    request_fingerprint: str | None = None,
) -> NutritionObservation | None:
    row = session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == OBSERVATION_EVENT,
            WellnessEvent.source_provider == SOURCE_PROVIDER,
            WellnessEvent.raw_object_id == storage_object_id,
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    if (
        request_fingerprint is not None
        and (
            not isinstance(row.derived_from, dict)
            or row.derived_from.get("request_fingerprint")
            != request_fingerprint
        )
    ):
        raise NutritionRepositoryError(
            "media was already analyzed with different capture metadata"
        )
    return get_observation(session, uuid.UUID(row.source_record_id))


def _structured_observation(
    observation: NutritionObservation,
) -> NutritionObservation:
    return replace(
        observation,
        capture=replace(
            observation.capture,
            media_path="",
            location=None,
        ),
        warnings=(),
        items=tuple(
            replace(
                item,
                serving=replace(item.serving, evidence_text=None),
                caffeine=replace(item.caffeine, evidence_text=None),
                nutrients=tuple(
                    replace(
                        nutrient,
                        amount=replace(
                            nutrient.amount,
                            evidence_text=None,
                        ),
                    )
                    for nutrient in item.nutrients
                ),
                label_text_candidates=(),
                product_code_candidates=(),
                warnings=(),
            )
            for item in observation.items
        ),
    )


def persist_observation(
    session: Session,
    settings: Settings,
    observation: NutritionObservation,
    *,
    request_fingerprint: str,
) -> WellnessEvent:
    lock_activity_write_plane(session)
    ensure_default_policies(session)
    source_record_id = str(observation.observation_id)
    existing = session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == OBSERVATION_EVENT,
            WellnessEvent.source_provider == SOURCE_PROVIDER,
            WellnessEvent.source_record_id == source_record_id,
        )
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        media_object_id = session.scalar(
            select(StorageObject.id).where(
                StorageObject.relative_path
                == observation.capture.media_path
            )
        )
        _validate_observation_retry(
            existing,
            media_object_id=media_object_id,
            request_fingerprint=request_fingerprint,
        )
        return existing

    obj = storage_object_for_media(session, observation.capture.media_path)
    if obj is None:
        raise NutritionRepositoryError("media storage object not found")
    if not (obj.content_type or "").startswith("image/"):
        raise NutritionRepositoryError("nutrition observations require an image object")

    observed_at = _as_utc(observation.capture.captured_at)
    if observed_at > clock.utc_now() + MAX_CAPTURE_CLOCK_SKEW:
        raise NutritionRepositoryError(
            "captured_at cannot be more than 5 minutes in the future"
        )
    policy = _policy(session, "nutrition_observation")
    structured_expiry = _expiry(policy, observed_at)
    if (
        structured_expiry is not None
        and structured_expiry <= clock.utc_now()
    ):
        raise NutritionRepositoryError(
            "captured_at falls outside the observation retention window"
        )
    durable_observation = _structured_observation(observation)
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
            "warning_count": len(observation.warnings),
        },
        confidence=confidence_score(observation.confidence),
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=structured_expiry,
        payload=observation_to_payload(durable_observation),
        raw_object_id=obj.id,
        derived_from={
            "storage_object_id": str(obj.id),
            "request_fingerprint": request_fingerprint,
        },
    )
    raw_policy = _policy(session, "nutrition_media")
    raw_event = WellnessEvent(
        event_type=RAW_OBSERVATION_EVENT,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=_as_utc(observation.vision.analyzed_at),
        timezone=observation.capture.timezone,
        source_provider=RAW_SOURCE_PROVIDER,
        source_device=observation.capture.source,
        source_record_id=source_record_id,
        capture_method="photo",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=raw_policy.id,
        expires_at=_expiry(raw_policy, observed_at),
        payload={"observation": observation_to_payload(observation)},
        raw_object_id=obj.id,
        derived_from={
            "observation_id": source_record_id,
            "request_fingerprint": request_fingerprint,
        },
    )
    try:
        with session.begin_nested():
            session.add_all((event, raw_event))
            session.flush()
            classify_storage_object(
                session,
                obj,
                data_class="nutrition_media",
                observed_at=observed_at,
                safe_to_purge=True,
            )
    except IntegrityError:
        existing = session.scalar(
            select(WellnessEvent)
            .where(
                WellnessEvent.event_type == OBSERVATION_EVENT,
                WellnessEvent.raw_object_id == obj.id,
            )
            .execution_options(populate_existing=True)
        )
        if existing is None:
            raise NutritionRepositoryError(
                "media already belongs to another nutrition observation"
            )
        _validate_observation_retry(
            existing,
            media_object_id=obj.id,
            request_fingerprint=request_fingerprint,
        )
        return existing
    return event


def get_observation(
    session: Session, observation_id: uuid.UUID
) -> NutritionObservation | None:
    row = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == OBSERVATION_EVENT,
            WellnessEvent.source_provider == SOURCE_PROVIDER,
            WellnessEvent.source_record_id == str(observation_id),
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
        )
    )
    if row is None:
        return None
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == RAW_OBSERVATION_EVENT,
            WellnessEvent.source_provider == RAW_SOURCE_PROVIDER,
            WellnessEvent.source_record_id == str(observation_id),
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
        )
    )
    if raw is not None and isinstance(raw.payload.get("observation"), dict):
        return observation_from_payload(raw.payload["observation"])
    return observation_from_payload(row.payload)


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
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
        )
        .order_by(WellnessEvent.observed_at.desc(), WellnessEvent.created_at.desc())
        .limit(limit)
    )
    if start is not None:
        statement = statement.where(WellnessEvent.observed_at >= _as_utc(start))
    if end is not None:
        statement = statement.where(WellnessEvent.observed_at < _as_utc(end))
    observations = []
    for row in session.scalars(statement):
        observation = get_observation(
            session,
            uuid.UUID(row.source_record_id),
        )
        if observation is not None:
            observations.append(observation)
    return observations


def persist_caffeine_confirmation(
    session: Session, confirmation: CaffeineConfirmation
) -> WellnessEvent:
    lock_activity_write_plane(session)
    ensure_default_policies(session)
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
    lock_activity_write_plane(session)
    ensure_default_policies(session)
    existing = session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.source_provider == "user-nutrition-review",
            WellnessEvent.source_record_id == str(review.review_id),
        )
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        stored = nutrition_review_from_payload(existing.payload)
        if not _same_nutrition_review(stored, review):
            raise NutritionRepositoryError(
                "nutrition review operation_id was already used with different input"
            )
        if _event_is_expired(existing):
            raise NutritionRepositoryError(
                "expired nutrition review cannot be retried"
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
            select(WellnessEvent)
            .where(
                WellnessEvent.source_provider == "user-nutrition-review",
                WellnessEvent.source_record_id == str(review.review_id),
            )
            .execution_options(populate_existing=True)
        )
        if existing is None:
            raise
        stored = nutrition_review_from_payload(existing.payload)
        if not _same_nutrition_review(stored, review):
            raise NutritionRepositoryError(
                "nutrition review operation_id was already used with different input"
            )
        if _event_is_expired(existing):
            raise NutritionRepositoryError(
                "expired nutrition review cannot be retried"
            )
        return existing
    return event


def persist_daily_confirmation(
    session: Session, confirmation: DailyIntakeConfirmation
) -> WellnessEvent:
    lock_activity_write_plane(session)
    ensure_default_policies(session)
    start, end = local_day_bounds(confirmation.local_date, confirmation.timezone)
    day_ids = {
        uuid.UUID(value)
        for value in session.scalars(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.event_type == OBSERVATION_EVENT,
                WellnessEvent.source_provider == SOURCE_PROVIDER,
                WellnessEvent.observed_at >= start,
                WellnessEvent.observed_at < end,
                or_(
                    WellnessEvent.expires_at.is_(None),
                    WellnessEvent.expires_at > clock.utc_now(),
                ),
            )
        )
    }
    supplied_ids = set(confirmation.observation_ids)
    outcome_states = intake_outcome_states_for_day(
        session,
        start=start,
        end=end,
    )
    day_outcome_ids = {
        outcome.outcome_id for _, outcome in outcome_states
    }
    supplied_outcome_ids = set(confirmation.outcome_ids)
    unknown = supplied_ids - day_ids
    if unknown:
        raise NutritionRepositoryError(
            "nutrition observations are not part of the confirmed local day: "
            + ", ".join(sorted(str(value) for value in unknown))
        )
    unknown_outcomes = supplied_outcome_ids - day_outcome_ids
    if unknown_outcomes:
        raise NutritionRepositoryError(
            "intake outcomes do not affect the confirmed local day: "
            + ", ".join(sorted(str(value) for value in unknown_outcomes))
        )
    if confirmation.total_intake_complete and supplied_ids != day_ids:
        raise NutritionRepositoryError(
            "complete-day confirmation must include every observation for that local day"
        )
    if (
        confirmation.total_intake_complete
        and supplied_outcome_ids != day_outcome_ids
    ):
        raise NutritionRepositoryError(
            "complete-day confirmation must include every latest intake "
            "outcome affecting that local day"
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
            "observation_ids": [str(value) for value in confirmation.observation_ids],
            "outcome_ids": [str(value) for value in confirmation.outcome_ids],
        },
    )
    session.add(event)
    session.flush()
    return event


def latest_intake_outcome_states(
    session: Session,
) -> dict[uuid.UUID, tuple[WellnessEvent, IntakeOutcome]]:
    rows = session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == INTAKE_OUTCOME_EVENT,
            WellnessEvent.source_provider == INTAKE_OUTCOME_PROVIDER,
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    latest: dict[uuid.UUID, tuple[WellnessEvent, IntakeOutcome]] = {}
    for row in rows:
        outcome = outcome_from_payload(row.payload)
        latest.setdefault(outcome.interaction_id, (row, outcome))
    return latest


def intake_outcome_states_for_day(
    session: Session,
    *,
    start: datetime,
    end: datetime,
) -> list[tuple[WellnessEvent, IntakeOutcome]]:
    rows = session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == INTAKE_OUTCOME_EVENT,
            WellnessEvent.source_provider == INTAKE_OUTCOME_PROVIDER,
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    latest: dict[uuid.UUID, tuple[WellnessEvent, IntakeOutcome]] = {}
    affected_interactions: set[uuid.UUID] = set()
    for row in rows:
        outcome = outcome_from_payload(row.payload)
        latest.setdefault(outcome.interaction_id, (row, outcome))
        if (
            outcome.status is IntakeOutcomeStatus.CONSUMED
            and outcome.consumed_at is not None
            and start <= _as_utc(outcome.consumed_at) < end
        ):
            affected_interactions.add(outcome.interaction_id)
        if (
            outcome.intake_snapshot is not None
            and start <= _as_utc(outcome.intake_snapshot.observed_at) < end
        ):
            affected_interactions.add(outcome.interaction_id)
    return [
        latest[interaction_id]
        for interaction_id in affected_interactions
    ]


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
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
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
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
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
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > clock.utc_now(),
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    for row in rows:
        confirmation = daily_confirmation_from_payload(row.payload)
        if confirmation.local_date == local_date and confirmation.timezone == timezone:
            return confirmation
    return None


def local_day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    tz = parse_timezone(timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)
