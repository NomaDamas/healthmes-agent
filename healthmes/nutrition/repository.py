"""WellnessEvent-backed persistence for sake nutrition observations."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from re import fullmatch
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
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
from healthmes.nutrition.intake_contracts import (
    IntakeOutcome,
    outcome_from_payload,
)
from healthmes.nutrition.ledger_lock import lock_nutrition_ledger
from healthmes.nutrition.operation_integrity import (
    RESULT_PAYLOAD_DIGEST_FIELD,
    is_sha256_digest,
    result_payload_digest,
)
from healthmes.nutrition.schema import (
    CORE_NUTRIENT_UNITS,
    SUPPORTED_NUTRIENT_UNITS,
)
from healthmes.storage import (
    classify_storage_object,
    retention_policies_for_write,
    retention_policy_for_write,
)
from healthmes.store import RetentionPolicy, StorageObject, WellnessEvent

OBSERVATION_EVENT = "nutrition.observation.v1"
CONFIRMATION_EVENT = "nutrition.confirmation.v1"
REVIEW_EVENT = "nutrition.review.v1"
DAILY_CONFIRMATION_EVENT = "nutrition.daily-confirmation.v1"
SOURCE_PROVIDER = "sake-vlm"
RAW_OBSERVATION_EVENT = "nutrition.observation-raw.v1"
RAW_SOURCE_PROVIDER = "sake-vlm-raw"
INTAKE_OUTCOME_EVENT = "nutrition.intake-outcome.v1"
INTAKE_OUTCOME_PROVIDER = "nutrition-intake-outcome"
INTAKE_INTERACTION_EVENT = "nutrition.interaction.v1"
INTAKE_INTERACTION_PROVIDER = "nutrition-interaction"
INTERACTION_TRANSITION_EVENT = "nutrition.interaction-transition.v1"
INTERACTION_TRANSITION_PROVIDER = "nutrition-interaction-transition"
OPERATION_EVENT = "nutrition.operation.v1"
OPERATION_PROVIDER = "nutrition-operation"
OUTCOME_TRANSITION_INTERACTION_OBSERVED_AT = "interaction_observed_at"
OUTCOME_TRANSITION_CONSUMED_AT = "outcome_consumed_at"
MAX_CAPTURE_CLOCK_SKEW = timedelta(minutes=5)
_OPERATION_RESULT_ID_FIELDS = {
    "caffeine_confirmation": "confirmation_id",
    "nutrition_review": "review_id",
    "daily_intake_confirmation": "confirmation_id",
}


class NutritionRepositoryError(RuntimeError):
    pass


class NutritionOperationConflict(NutritionRepositoryError):
    pass


class NutritionStorageIntegrityError(NutritionRepositoryError):
    pass


class InvalidInteractionTransitionChain(NutritionStorageIntegrityError):
    pass


def _stored_payload(
    parser: Callable[[dict[str, Any]], Any],
    payload: dict[str, Any],
    *,
    record_name: str,
) -> Any:
    try:
        return parser(payload)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise NutritionStorageIntegrityError(f"stored {record_name} payload is malformed") from exc


def _same_caffeine_confirmation(
    stored: CaffeineConfirmation,
    incoming: CaffeineConfirmation,
) -> bool:
    return (
        stored.confirmation_id == incoming.confirmation_id
        and stored.observation_id == incoming.observation_id
        and stored.status is incoming.status
        and stored.source == incoming.source
        and stored.items == incoming.items
    )


def _same_nutrition_review(stored: NutritionReview, incoming: NutritionReview) -> bool:
    return (
        stored.review_id == incoming.review_id
        and stored.observation_id == incoming.observation_id
        and stored.status is incoming.status
        and stored.source == incoming.source
        and stored.items == incoming.items
    )


def _same_daily_confirmation(
    stored: DailyIntakeConfirmation,
    incoming: DailyIntakeConfirmation,
) -> bool:
    return (
        stored.confirmation_id == incoming.confirmation_id
        and stored.local_date == incoming.local_date
        and stored.timezone == incoming.timezone
        and stored.observation_ids == incoming.observation_ids
        and stored.total_intake_complete is incoming.total_intake_complete
        and stored.source == incoming.source
        and stored.outcome_ids == incoming.outcome_ids
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def outcome_transition_projection_payload(
    *,
    interaction_observed_at: datetime,
    consumed_at: datetime | None,
) -> dict[str, str | None]:
    """Build the non-nutrient projection needed after outcome result expiry."""

    return {
        OUTCOME_TRANSITION_INTERACTION_OBSERVED_AT: _as_utc(interaction_observed_at).isoformat(),
        OUTCOME_TRANSITION_CONSUMED_AT: (
            _as_utc(consumed_at).isoformat() if consumed_at is not None else None
        ),
    }


def _parse_transition_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _outcome_transition_projection(
    event: WellnessEvent,
) -> tuple[bool, tuple[datetime, datetime | None] | None]:
    payload = event.payload
    fields_present = (
        OUTCOME_TRANSITION_INTERACTION_OBSERVED_AT in payload,
        OUTCOME_TRANSITION_CONSUMED_AT in payload,
    )
    if not any(fields_present):
        return False, None
    if not all(fields_present):
        return True, None
    interaction_observed_at = _parse_transition_datetime(
        payload.get(OUTCOME_TRANSITION_INTERACTION_OBSERVED_AT)
    )
    raw_consumed_at = payload.get(OUTCOME_TRANSITION_CONSUMED_AT)
    consumed_at = None if raw_consumed_at is None else _parse_transition_datetime(raw_consumed_at)
    status = payload.get("mutation_status")
    if (
        interaction_observed_at is None
        or (raw_consumed_at is not None and consumed_at is None)
        or (status == "consumed") != (consumed_at is not None)
    ):
        return True, None
    return True, (interaction_observed_at, consumed_at)


def _transition_source_interaction_id(
    event: WellnessEvent,
) -> uuid.UUID | None:
    if (
        event.event_type != INTERACTION_TRANSITION_EVENT
        or event.source_provider != INTERACTION_TRANSITION_PROVIDER
    ):
        return None
    raw_interaction_id, separator, raw_revision = event.source_record_id.rpartition(":")
    if not separator:
        return None
    try:
        interaction_id = uuid.UUID(raw_interaction_id)
    except ValueError:
        return None
    if raw_interaction_id != str(interaction_id):
        return None
    return interaction_id


def interaction_transition_identity(
    event: WellnessEvent,
) -> tuple[uuid.UUID, int] | None:
    """Return a canonical source-backed and semantically valid identity."""

    if _is_maintenance_quarantined(event):
        return None
    interaction_id = _transition_source_interaction_id(event)
    if interaction_id is None:
        return None
    _, _, raw_revision = event.source_record_id.rpartition(":")
    try:
        revision = int(raw_revision)
    except ValueError:
        return None
    mutation_kind = event.payload.get("mutation_kind")
    mutation_status = event.payload.get("mutation_status")
    accepted_statuses = {
        "review": {"confirmed", "corrected", "rejected"},
        "outcome": {"consumed", "not_consumed", "cancelled"},
    }
    raw_operation_id = event.payload.get("operation_id")
    if not isinstance(raw_operation_id, str):
        return None
    try:
        operation_id = uuid.UUID(raw_operation_id)
    except ValueError:
        return None
    projection_present, projection = _outcome_transition_projection(event)
    if (
        revision < 1
        or raw_revision != str(revision)
        or event.payload.get("interaction_id") != str(interaction_id)
        or type(event.payload.get("revision")) is not int
        or event.payload.get("revision") != revision
        or not isinstance(mutation_kind, str)
        or mutation_status
        not in accepted_statuses.get(
            mutation_kind,
            set(),
        )
        or raw_operation_id != str(operation_id)
        or (mutation_kind == "outcome" and projection_present and projection is None)
    ):
        return None
    return interaction_id, revision


def validated_interaction_transition_chain(
    events: list[WellnessEvent],
    interaction_id: uuid.UUID,
) -> list[WellnessEvent] | None:
    """Return a complete immutable revision chain or fail closed."""

    ordered = sorted(
        events,
        key=lambda event: (
            interaction_transition_identity(event)
            or (
                interaction_id,
                0,
            ),
            _as_utc(event.recorded_at),
            str(event.id),
        ),
    )
    seen_operations: set[tuple[str, str]] = set()
    outcome_seen = False
    for expected_revision, event in enumerate(ordered, start=1):
        identity = interaction_transition_identity(event)
        if identity != (interaction_id, expected_revision):
            return None
        mutation_kind = event.payload["mutation_kind"]
        if mutation_kind == "review" and outcome_seen:
            return None
        operation_id = event.payload["operation_id"]
        operation_identity = (mutation_kind, operation_id)
        if operation_identity in seen_operations:
            return None
        seen_operations.add(operation_identity)
        if mutation_kind == "outcome":
            outcome_seen = True
    return ordered


def _validated_interaction_transition_groups(
    session: Session,
    interaction_ids: set[uuid.UUID] | None = None,
) -> dict[uuid.UUID, list[WellnessEvent]]:
    if interaction_ids is not None and not interaction_ids:
        return {}
    statement = select(WellnessEvent).where(
        WellnessEvent.event_type == INTERACTION_TRANSITION_EVENT,
        WellnessEvent.source_provider == INTERACTION_TRANSITION_PROVIDER,
    )
    if interaction_ids is not None:
        statement = statement.where(
            or_(*(WellnessEvent.source_record_id.like(f"{value}:%") for value in interaction_ids))
        )

    grouped: dict[uuid.UUID, list[WellnessEvent]] = {}
    for row in session.scalars(statement):
        interaction_id = _transition_source_interaction_id(row)
        if interaction_id is None:
            continue
        if interaction_ids is not None and interaction_id not in interaction_ids:
            continue
        grouped.setdefault(interaction_id, []).append(row)

    validated: dict[uuid.UUID, list[WellnessEvent]] = {}
    invalid_interaction_ids: list[uuid.UUID] = []
    for interaction_id, events in grouped.items():
        chain = validated_interaction_transition_chain(
            events,
            interaction_id,
        )
        if chain is None:
            invalid_interaction_ids.append(interaction_id)
        else:
            validated[interaction_id] = chain
    if invalid_interaction_ids:
        values = ", ".join(sorted(str(value) for value in invalid_interaction_ids))
        raise InvalidInteractionTransitionChain(f"invalid interaction transition chain: {values}")
    return validated


def latest_interaction_transitions(
    session: Session,
    *,
    mutation_kind: str,
    interaction_ids: set[uuid.UUID] | None = None,
) -> dict[uuid.UUID, WellnessEvent]:
    """Return the highest durable transition revision for each interaction."""

    latest: dict[uuid.UUID, WellnessEvent] = {}
    for interaction_id, chain in _validated_interaction_transition_groups(
        session,
        interaction_ids,
    ).items():
        matching = [event for event in chain if event.payload.get("mutation_kind") == mutation_kind]
        if matching:
            latest[interaction_id] = matching[-1]
    return latest


def _policy(session: Session, data_class: str) -> RetentionPolicy:
    try:
        return retention_policy_for_write(session, data_class)
    except ValueError as exc:  # pragma: no cover - internal constants own this
        raise NutritionRepositoryError(str(exc)) from exc


def _expiry(policy: RetentionPolicy, observed_at: datetime) -> datetime | None:
    if not policy.enabled or policy.retention_days is None:
        return None
    return observed_at + timedelta(days=policy.retention_days)


def _validate_operation_fingerprint(operation_fingerprint: str) -> None:
    if fullmatch(r"[0-9a-f]{64}", operation_fingerprint) is None:
        raise NutritionRepositoryError("operation_fingerprint must be a lowercase SHA-256 digest")


def _event_by_source_identity(
    session: Session,
    *,
    source_provider: str,
    source_record_id: str,
) -> WellnessEvent | None:
    return session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == source_provider,
            WellnessEvent.source_record_id == source_record_id,
        )
    )


def _operation_record_id(
    operation_prefix: str,
    operation_id: uuid.UUID,
) -> str:
    return f"{operation_prefix}:{operation_id}"


def _is_maintenance_quarantined(event: WellnessEvent) -> bool:
    return isinstance(event.quality_flags, dict) and "maintenance_quarantine" in event.quality_flags


def _validate_result_payload_digest(
    marker: WellnessEvent,
    result: WellnessEvent,
    *,
    operation_name: str,
) -> None:
    if RESULT_PAYLOAD_DIGEST_FIELD not in marker.payload:
        return
    digest = marker.payload.get(RESULT_PAYLOAD_DIGEST_FIELD)
    if not is_sha256_digest(digest) or digest != result_payload_digest(result.payload):
        raise NutritionStorageIntegrityError(
            f"stored {operation_name} result payload digest is invalid"
        )


def _validate_operation_result_identity(
    result: WellnessEvent,
    *,
    operation_id: uuid.UUID,
    operation_kind: str,
    operation_name: str,
) -> None:
    operation_id_field = _OPERATION_RESULT_ID_FIELDS.get(operation_kind)
    if (
        operation_id_field is None
        or not isinstance(result.payload, dict)
        or result.payload.get(operation_id_field) != str(operation_id)
    ):
        raise NutritionOperationConflict(
            f"stored {operation_name} identity is invalid; retry is blocked"
        )


def _existing_operation_result(
    session: Session,
    *,
    operation_id: uuid.UUID,
    operation_fingerprint: str,
    operation_kind: str,
    operation_name: str,
    operation_prefix: str,
    result_event_type: str,
    result_source_provider: str,
) -> WellnessEvent | None:
    marker = _event_by_source_identity(
        session,
        source_provider=OPERATION_PROVIDER,
        source_record_id=_operation_record_id(
            operation_prefix,
            operation_id,
        ),
    )
    result = _event_by_source_identity(
        session,
        source_provider=result_source_provider,
        source_record_id=_operation_record_id(
            operation_prefix,
            operation_id,
        ),
    )
    if result is not None and result.event_type != result_event_type:
        raise NutritionOperationConflict(
            f"{operation_name} operation_id was already used by another nutrition write"
        )
    if result is None:
        legacy_result = _event_by_source_identity(
            session,
            source_provider=result_source_provider,
            source_record_id=str(operation_id),
        )
        if legacy_result is not None and legacy_result.event_type == result_event_type:
            result = legacy_result
    if result is not None and _is_maintenance_quarantined(result):
        raise NutritionOperationConflict(f"quarantined {operation_name} cannot be retried")
    if result is not None:
        _validate_operation_result_identity(
            result,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_name=operation_name,
        )
    if marker is None:
        if result is None:
            return None
        if result.expires_at is not None and _as_utc(result.expires_at) <= datetime.now(UTC):
            raise NutritionOperationConflict(f"expired {operation_name} cannot be retried")
        # Legacy result events predate durable operation markers. The caller
        # performs the operation-specific payload equality check before
        # accepting this as an exact retry.
        return result
    if (
        marker.payload.get("operation_kind") != operation_kind
        or marker.payload.get("operation_id") != str(operation_id)
        or marker.payload.get("operation_state") != "completed"
        or _is_maintenance_quarantined(marker)
    ):
        raise NutritionOperationConflict(
            f"{operation_name} operation_id was already used by another nutrition write"
        )
    if (
        marker.payload.get("operation_fingerprint") is None
        and marker.payload.get("legacy_backfill") is True
    ):
        if result is None or (
            result.expires_at is not None and _as_utc(result.expires_at) <= datetime.now(UTC)
        ):
            raise NutritionOperationConflict(f"expired {operation_name} cannot be retried")
        _validate_result_payload_digest(
            marker,
            result,
            operation_name=operation_name,
        )
        # The operation-specific caller validates the retained legacy payload.
        return result
    if marker.payload.get("operation_fingerprint") != operation_fingerprint:
        raise NutritionOperationConflict(
            f"{operation_name} operation_id was already used with different input"
        )
    if result is None or (
        result.expires_at is not None and _as_utc(result.expires_at) <= datetime.now(UTC)
    ):
        raise NutritionOperationConflict(f"expired {operation_name} cannot be retried")
    _validate_result_payload_digest(
        marker,
        result,
        operation_name=operation_name,
    )
    return result


def _operation_marker(
    *,
    operation_id: uuid.UUID,
    operation_fingerprint: str,
    operation_kind: str,
    operation_prefix: str,
    recorded_at: datetime,
    timezone: str | None,
    result_payload: dict[str, Any],
) -> WellnessEvent:
    return WellnessEvent(
        event_type=OPERATION_EVENT,
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone=timezone,
        source_provider=OPERATION_PROVIDER,
        source_device=None,
        source_record_id=_operation_record_id(
            operation_prefix,
            operation_id,
        ),
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": operation_kind,
            "operation_id": str(operation_id),
            "operation_fingerprint": operation_fingerprint,
            "operation_state": "completed",
            RESULT_PAYLOAD_DIGEST_FIELD: result_payload_digest(result_payload),
        },
        derived_from=None,
    )


def _persist_operation_result(
    session: Session,
    event: WellnessEvent,
    *,
    operation_id: uuid.UUID,
    operation_fingerprint: str,
    operation_kind: str,
    operation_name: str,
    operation_prefix: str,
    result_event_type: str,
    result_source_provider: str,
    recorded_at: datetime,
    timezone: str | None,
) -> WellnessEvent:
    marker = _operation_marker(
        operation_id=operation_id,
        operation_fingerprint=operation_fingerprint,
        operation_kind=operation_kind,
        operation_prefix=operation_prefix,
        recorded_at=recorded_at,
        timezone=timezone,
        result_payload=event.payload,
    )
    try:
        with session.begin_nested():
            session.add_all((event, marker))
            session.flush()
    except IntegrityError:
        existing = _existing_operation_result(
            session,
            operation_id=operation_id,
            operation_fingerprint=operation_fingerprint,
            operation_kind=operation_kind,
            operation_name=operation_name,
            operation_prefix=operation_prefix,
            result_event_type=result_event_type,
            result_source_provider=result_source_provider,
        )
        if existing is None:
            raise
        return existing
    return event


def storage_object_for_media(session: Session, media_path: str) -> StorageObject | None:
    now = datetime.now(UTC)
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
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc())
        .limit(1)
    )
    if row is None or _is_maintenance_quarantined(row):
        return None
    if request_fingerprint is not None and (
        not isinstance(row.derived_from, dict)
        or row.derived_from.get("request_fingerprint") != request_fingerprint
    ):
        raise NutritionRepositoryError("media was already analyzed with different capture metadata")
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
    lock_nutrition_ledger(session)
    retention_policies_for_write(
        session,
        {"nutrition_media", "nutrition_observation"},
    )
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
    if observed_at > datetime.now(UTC) + MAX_CAPTURE_CLOCK_SKEW:
        raise NutritionRepositoryError("captured_at cannot be more than 5 minutes in the future")
    policy = _policy(session, "nutrition_observation")
    structured_expiry = _expiry(policy, observed_at)
    if structured_expiry is not None and structured_expiry <= datetime.now(UTC):
        raise NutritionRepositoryError("captured_at falls outside the observation retention window")
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
            select(WellnessEvent).where(
                WellnessEvent.event_type == OBSERVATION_EVENT,
                WellnessEvent.raw_object_id == obj.id,
            )
        )
        if existing is None:
            raise NutritionRepositoryError("media already belongs to another nutrition observation")
        if (
            not isinstance(existing.derived_from, dict)
            or existing.derived_from.get("request_fingerprint") != request_fingerprint
        ):
            raise NutritionRepositoryError(
                "media was already analyzed with different capture metadata"
            )
        return existing
    return event


def get_observation(session: Session, observation_id: uuid.UUID) -> NutritionObservation | None:
    row = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == OBSERVATION_EVENT,
            WellnessEvent.source_provider == SOURCE_PROVIDER,
            WellnessEvent.source_record_id == str(observation_id),
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
    )
    if row is None or _is_maintenance_quarantined(row):
        return None
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == RAW_OBSERVATION_EVENT,
            WellnessEvent.source_provider == RAW_SOURCE_PROVIDER,
            WellnessEvent.source_record_id == str(observation_id),
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
    )
    if (
        raw is not None
        and not _is_maintenance_quarantined(raw)
        and isinstance(raw.payload.get("observation"), dict)
    ):
        return _stored_payload(
            observation_from_payload,
            raw.payload["observation"],
            record_name="nutrition observation",
        )
    return _stored_payload(
        observation_from_payload,
        row.payload,
        record_name="nutrition observation",
    )


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
                WellnessEvent.expires_at > datetime.now(UTC),
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
        if _is_maintenance_quarantined(row):
            continue
        try:
            observation_id = uuid.UUID(row.source_record_id)
        except (TypeError, ValueError) as exc:
            raise NutritionStorageIntegrityError(
                "stored nutrition observation identity is malformed"
            ) from exc
        if row.source_record_id != str(observation_id):
            raise NutritionStorageIntegrityError(
                "stored nutrition observation identity is noncanonical"
            )
        observation = get_observation(session, observation_id)
        if observation is not None:
            observations.append(observation)
    return observations


def persist_caffeine_confirmation(
    session: Session,
    confirmation: CaffeineConfirmation,
    *,
    operation_fingerprint: str,
) -> WellnessEvent:
    lock_nutrition_ledger(session)
    _validate_operation_fingerprint(operation_fingerprint)
    existing = _existing_operation_result(
        session,
        operation_id=confirmation.confirmation_id,
        operation_fingerprint=operation_fingerprint,
        operation_kind="caffeine_confirmation",
        operation_name="caffeine confirmation",
        operation_prefix="caffeine-confirmation",
        result_event_type=CONFIRMATION_EVENT,
        result_source_provider="user-confirmation",
    )
    if existing is not None:
        try:
            stored = caffeine_confirmation_from_payload(existing.payload)
        except (TypeError, ValueError) as exc:
            raise NutritionOperationConflict(
                "stored caffeine confirmation result is malformed; operation_id retry is blocked"
            ) from exc
        if not _same_caffeine_confirmation(stored, confirmation):
            raise NutritionOperationConflict(
                "caffeine confirmation operation_id was already used with different input"
            )
        return existing
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
            raise NutritionRepositoryError("rejected observations cannot contain confirmed items")
    else:
        expected_indexes = set(range(len(observation.items)))
        confirmed_indexes = {item.item_index for item in confirmation.items}
        if len(confirmed_indexes) != len(confirmation.items):
            raise NutritionRepositoryError("confirmation item indexes must not contain duplicates")
        if confirmed_indexes != expected_indexes:
            raise NutritionRepositoryError(
                "confirmation must provide caffeine_mg for every observation item"
            )
        if any(
            not isfinite(item.caffeine_mg) or item.caffeine_mg < 0 for item in confirmation.items
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
        source_record_id=_operation_record_id(
            "caffeine-confirmation",
            confirmation.confirmation_id,
        ),
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
    return _persist_operation_result(
        session,
        event,
        operation_id=confirmation.confirmation_id,
        operation_fingerprint=operation_fingerprint,
        operation_kind="caffeine_confirmation",
        operation_name="caffeine confirmation",
        operation_prefix="caffeine-confirmation",
        result_event_type=CONFIRMATION_EVENT,
        result_source_provider="user-confirmation",
        recorded_at=observed_at,
        timezone=None,
    )


def _validate_review_estimate(estimate: Estimate) -> None:
    if not estimate.unit.strip() or len(estimate.unit) > 32:
        raise NutritionRepositoryError(
            "reviewed estimate unit must contain between 1 and 32 characters"
        )
    if estimate.evidence_text is not None and len(estimate.evidence_text) > 500:
        raise NutritionRepositoryError(
            "reviewed estimate evidence_text cannot exceed 500 characters"
        )
    if estimate.estimation_basis is not None and len(estimate.estimation_basis) > 64:
        raise NutritionRepositoryError(
            "reviewed estimate estimation_basis cannot exceed 64 characters"
        )
    values = (estimate.exact, estimate.minimum, estimate.maximum)
    if any(value is not None and (not isfinite(value) or value < 0) for value in values):
        raise NutritionRepositoryError(
            "reviewed nutrition estimates must be finite and non-negative"
        )
    if estimate.kind is EstimateKind.EXACT:
        valid = estimate.exact is not None and estimate.minimum is None and estimate.maximum is None
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


def validate_nutrition_review(
    session: Session,
    review: NutritionReview,
) -> NutritionObservation:
    """Validate a review before either staging or persisting it."""

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
            raise NutritionRepositoryError("at most 50 reviewed nutrition items are accepted")
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
            if len(item.warnings) > 20 or any(len(warning) > 500 for warning in item.warnings):
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
                if fullmatch(r"[a-z][a-z0-9_]{0,63}", nutrient.nutrient) is None:
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
    return observation


def persist_nutrition_review(
    session: Session,
    review: NutritionReview,
    *,
    operation_fingerprint: str,
) -> WellnessEvent:
    lock_nutrition_ledger(session)
    _validate_operation_fingerprint(operation_fingerprint)
    existing = _existing_operation_result(
        session,
        operation_id=review.review_id,
        operation_fingerprint=operation_fingerprint,
        operation_kind="nutrition_review",
        operation_name="nutrition review",
        operation_prefix="nutrition-review",
        result_event_type=REVIEW_EVENT,
        result_source_provider="user-nutrition-review",
    )
    if existing is not None:
        try:
            stored = nutrition_review_from_payload(existing.payload)
        except (TypeError, ValueError) as exc:
            raise NutritionOperationConflict(
                "stored nutrition review result is malformed; operation_id retry is blocked"
            ) from exc
        if not _same_nutrition_review(stored, review):
            raise NutritionOperationConflict(
                "nutrition review operation_id was already used with different input"
            )
        return existing
    observation = validate_nutrition_review(session, review)

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
        source_record_id=_operation_record_id(
            "nutrition-review",
            review.review_id,
        ),
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
    return _persist_operation_result(
        session,
        event,
        operation_id=review.review_id,
        operation_fingerprint=operation_fingerprint,
        operation_kind="nutrition_review",
        operation_name="nutrition review",
        operation_prefix="nutrition-review",
        result_event_type=REVIEW_EVENT,
        result_source_provider="user-nutrition-review",
        recorded_at=reviewed_at,
        timezone=observation.capture.timezone,
    )


def unresolved_log_consumed_interaction_ids_for_day(
    session: Session,
    *,
    start: datetime,
    end: datetime,
) -> set[uuid.UUID]:
    """Return consumed-intent captures that still need an explicit outcome."""

    active_outcomes = set(
        latest_interaction_transitions(
            session,
            mutation_kind="outcome",
        )
    )
    active_outcomes.update(
        outcome.interaction_id for _, outcome in _active_intake_outcomes(session)
    )
    rows = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == INTAKE_INTERACTION_EVENT,
            WellnessEvent.source_provider == INTAKE_INTERACTION_PROVIDER,
            WellnessEvent.observed_at >= start,
            WellnessEvent.observed_at < end,
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
            WellnessEvent.payload["intent"].as_string() == "log_consumed",
        )
    )
    unresolved: set[uuid.UUID] = set()
    for row in rows:
        if _is_maintenance_quarantined(row):
            continue
        value = row.source_record_id
        try:
            interaction_id = uuid.UUID(value)
        except (TypeError, ValueError):
            continue
        if interaction_id not in active_outcomes:
            unresolved.add(interaction_id)
    return unresolved


def persist_daily_confirmation(
    session: Session,
    confirmation: DailyIntakeConfirmation,
    *,
    operation_fingerprint: str,
) -> WellnessEvent:
    lock_nutrition_ledger(session)
    _validate_operation_fingerprint(operation_fingerprint)
    existing = _existing_operation_result(
        session,
        operation_id=confirmation.confirmation_id,
        operation_fingerprint=operation_fingerprint,
        operation_kind="daily_intake_confirmation",
        operation_name="daily intake confirmation",
        operation_prefix="daily-confirmation",
        result_event_type=DAILY_CONFIRMATION_EVENT,
        result_source_provider="user-confirmation",
    )
    if existing is not None:
        try:
            stored = daily_confirmation_from_payload(existing.payload)
        except (TypeError, ValueError) as exc:
            raise NutritionOperationConflict(
                "stored daily intake confirmation result is malformed; "
                "operation_id retry is blocked"
            ) from exc
        if not _same_daily_confirmation(stored, confirmation):
            raise NutritionOperationConflict(
                "daily intake confirmation operation_id was already used with different input"
            )
        return existing

    start, end = local_day_bounds(confirmation.local_date, confirmation.timezone)
    day_ids: set[uuid.UUID] = set()
    for row in session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == OBSERVATION_EVENT,
            WellnessEvent.source_provider == SOURCE_PROVIDER,
            WellnessEvent.observed_at >= start,
            WellnessEvent.observed_at < end,
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
    ):
        if _is_maintenance_quarantined(row):
            continue
        try:
            observation_id = uuid.UUID(row.source_record_id)
        except (TypeError, ValueError):
            continue
        if row.source_record_id == str(observation_id):
            day_ids.add(observation_id)
    day_ids -= interaction_owned_observation_ids(session, day_ids)
    supplied_ids = set(confirmation.observation_ids)
    outcome_states, unavailable_outcome_ids = intake_outcome_ledger_for_day(
        session,
        start=start,
        end=end,
    )
    day_outcome_ids = {outcome.outcome_id for _, outcome in outcome_states}
    supplied_outcome_ids = set(confirmation.outcome_ids)
    unresolved_interaction_ids = unresolved_log_consumed_interaction_ids_for_day(
        session,
        start=start,
        end=end,
    )
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
    if confirmation.total_intake_complete and unavailable_outcome_ids:
        raise NutritionRepositoryError(
            "complete-day confirmation cannot verify retained outcome "
            "transitions whose result payload is unavailable: "
            + ", ".join(sorted(str(value) for value in unavailable_outcome_ids))
        )
    if confirmation.total_intake_complete and supplied_outcome_ids != day_outcome_ids:
        raise NutritionRepositoryError(
            "complete-day confirmation must include every latest intake "
            "outcome affecting that local day"
        )
    if confirmation.total_intake_complete and unresolved_interaction_ids:
        raise NutritionRepositoryError(
            "complete-day confirmation requires an outcome for every "
            "log_consumed interaction: "
            + ", ".join(sorted(str(value) for value in unresolved_interaction_ids))
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
        source_record_id=_operation_record_id(
            "daily-confirmation",
            confirmation.confirmation_id,
        ),
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
    return _persist_operation_result(
        session,
        event,
        operation_id=confirmation.confirmation_id,
        operation_fingerprint=operation_fingerprint,
        operation_kind="daily_intake_confirmation",
        operation_name="daily intake confirmation",
        operation_prefix="daily-confirmation",
        result_event_type=DAILY_CONFIRMATION_EVENT,
        result_source_provider="user-confirmation",
        recorded_at=observed_at,
        timezone=confirmation.timezone,
    )


def interaction_owned_observation_ids(
    session: Session,
    observation_ids: set[uuid.UUID],
) -> set[uuid.UUID]:
    """Return photo observations whose consumption state is owned by an interaction."""

    if not observation_ids:
        return set()
    raw_ids = {str(value) for value in observation_ids}
    rows = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == INTAKE_INTERACTION_EVENT,
            WellnessEvent.source_provider == INTAKE_INTERACTION_PROVIDER,
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
            WellnessEvent.payload["nutrition_observation_id"].as_string().in_(raw_ids),
        )
    )
    linked: set[uuid.UUID] = set()
    for row in rows:
        if _is_maintenance_quarantined(row):
            continue
        raw_observation_id = row.payload.get("nutrition_observation_id")
        try:
            observation_id = uuid.UUID(str(raw_observation_id))
        except (TypeError, ValueError):
            continue
        if observation_id in observation_ids:
            linked.add(observation_id)
    return linked


def _active_intake_outcomes(
    session: Session,
) -> list[tuple[WellnessEvent, IntakeOutcome]]:
    active: list[tuple[WellnessEvent, IntakeOutcome]] = []
    for row in session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == INTAKE_OUTCOME_EVENT,
            WellnessEvent.source_provider == INTAKE_OUTCOME_PROVIDER,
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
        .order_by(
            WellnessEvent.recorded_at.desc(),
            WellnessEvent.created_at.desc(),
        )
    ):
        if _is_maintenance_quarantined(row):
            continue
        active.append(
            (
                row,
                _stored_payload(
                    outcome_from_payload,
                    row.payload,
                    record_name="intake outcome",
                ),
            )
        )
    return active


def _outcome_affects_day(
    outcome: IntakeOutcome,
    *,
    start: datetime,
    end: datetime,
) -> bool:
    consumed_at = _as_utc(outcome.consumed_at) if outcome.consumed_at is not None else None
    interaction_observed_at = (
        _as_utc(outcome.intake_snapshot.observed_at)
        if outcome.intake_snapshot is not None
        else None
    )
    return bool(
        (consumed_at is not None and start <= consumed_at < end)
        or (interaction_observed_at is not None and start <= interaction_observed_at < end)
    )


def intake_outcome_ledger_for_day(
    session: Session,
    *,
    start: datetime,
    end: datetime,
) -> tuple[
    list[tuple[WellnessEvent, IntakeOutcome]],
    set[uuid.UUID],
]:
    """Return readable latest outcomes and unverifiable durable states."""

    rows = _active_intake_outcomes(session)
    by_operation_id = {str(outcome.outcome_id): (row, outcome) for row, outcome in rows}
    rows_by_interaction: dict[
        uuid.UUID,
        list[tuple[WellnessEvent, IntakeOutcome]],
    ] = {}
    for row, outcome in rows:
        rows_by_interaction.setdefault(
            outcome.interaction_id,
            [],
        ).append((row, outcome))

    states: list[tuple[WellnessEvent, IntakeOutcome]] = []
    unavailable: set[uuid.UUID] = set()
    transition_interactions: set[uuid.UUID] = set()
    for interaction_id, chain in _validated_interaction_transition_groups(session).items():
        outcome_transitions = [
            event for event in chain if event.payload.get("mutation_kind") == "outcome"
        ]
        if not outcome_transitions:
            continue
        transition_interactions.add(interaction_id)
        affects_day = False
        scope_unknown = False
        for transition in outcome_transitions:
            operation_id = transition.payload["operation_id"]
            projection_present, projection = _outcome_transition_projection(transition)
            if projection_present and projection is not None:
                interaction_observed_at, consumed_at = projection
                affects_day = affects_day or bool(
                    start <= interaction_observed_at < end
                    or (consumed_at is not None and start <= consumed_at < end)
                )
                continue
            result = by_operation_id.get(operation_id)
            if (
                result is None
                or result[1].interaction_id != interaction_id
                or result[1].intake_snapshot is None
            ):
                scope_unknown = True
                continue
            affects_day = affects_day or _outcome_affects_day(
                result[1],
                start=start,
                end=end,
            )

        latest_transition = outcome_transitions[-1]
        operation_id = uuid.UUID(latest_transition.payload["operation_id"])
        latest_state = by_operation_id.get(str(operation_id))
        if scope_unknown:
            unavailable.add(operation_id)
        if not affects_day and not scope_unknown:
            continue
        if latest_state is None or latest_state[1].interaction_id != interaction_id:
            unavailable.add(operation_id)
            continue
        if latest_transition.payload.get("mutation_status") != latest_state[1].status.value:
            raise InvalidInteractionTransitionChain(
                "interaction transition status does not match the intake "
                f"outcome payload: {interaction_id}"
            )
        states.append(latest_state)

    for interaction_id, interaction_rows in rows_by_interaction.items():
        if interaction_id in transition_interactions:
            continue
        if any(
            _outcome_affects_day(outcome, start=start, end=end) for _, outcome in interaction_rows
        ):
            states.append(interaction_rows[0])

    states.sort(
        key=lambda state: (
            _as_utc(state[1].confirmed_at),
            str(state[1].outcome_id),
        ),
        reverse=True,
    )
    return states, unavailable


def _latest_intake_outcomes_from_rows(
    session: Session,
    rows: list[tuple[WellnessEvent, IntakeOutcome]],
) -> dict[uuid.UUID, tuple[WellnessEvent, IntakeOutcome]]:
    legacy_latest: dict[
        uuid.UUID,
        tuple[WellnessEvent, IntakeOutcome],
    ] = {}
    by_operation_id: dict[
        str,
        tuple[WellnessEvent, IntakeOutcome],
    ] = {}
    for row, outcome in rows:
        legacy_latest.setdefault(outcome.interaction_id, (row, outcome))
        by_operation_id[str(outcome.outcome_id)] = (row, outcome)

    transitions = latest_interaction_transitions(
        session,
        mutation_kind="outcome",
        interaction_ids=set(legacy_latest),
    )
    latest: dict[uuid.UUID, tuple[WellnessEvent, IntakeOutcome]] = {}
    for interaction_id, legacy_state in legacy_latest.items():
        transition = transitions.get(interaction_id)
        if transition is None:
            latest[interaction_id] = legacy_state
            continue
        operation_id = transition.payload.get("operation_id")
        if not isinstance(operation_id, str):
            continue
        state = by_operation_id.get(operation_id)
        if state is None or state[1].interaction_id != interaction_id:
            continue
        if transition.payload.get("mutation_status") != state[1].status.value:
            raise InvalidInteractionTransitionChain(
                "interaction transition status does not match the intake "
                f"outcome payload: {interaction_id}"
            )
        latest[interaction_id] = state
    return latest


def latest_intake_outcome_state(
    session: Session,
    interaction_id: uuid.UUID,
) -> tuple[WellnessEvent, IntakeOutcome] | None:
    transition = latest_interaction_transitions(
        session,
        mutation_kind="outcome",
        interaction_ids={interaction_id},
    ).get(interaction_id)
    if transition is not None:
        operation_id = uuid.UUID(transition.payload["operation_id"])
        row = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == INTAKE_OUTCOME_EVENT,
                WellnessEvent.source_provider == INTAKE_OUTCOME_PROVIDER,
                WellnessEvent.source_record_id == str(operation_id),
                or_(
                    WellnessEvent.expires_at.is_(None),
                    WellnessEvent.expires_at > datetime.now(UTC),
                ),
            )
        )
        if row is None or _is_maintenance_quarantined(row):
            raise NutritionStorageIntegrityError("latest intake outcome result is unavailable")
        outcome = _stored_payload(
            outcome_from_payload,
            row.payload,
            record_name="intake outcome",
        )
        if (
            outcome.outcome_id != operation_id
            or outcome.interaction_id != interaction_id
            or transition.payload.get("mutation_status") != outcome.status.value
        ):
            raise NutritionStorageIntegrityError(
                "interaction transition status does not match the intake outcome payload"
            )
        return row, outcome

    rows: list[tuple[WellnessEvent, IntakeOutcome]] = []
    for row in session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == INTAKE_OUTCOME_EVENT,
            WellnessEvent.source_provider == INTAKE_OUTCOME_PROVIDER,
            WellnessEvent.payload["interaction_id"].as_string() == str(interaction_id),
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
        .order_by(
            WellnessEvent.recorded_at.desc(),
            WellnessEvent.created_at.desc(),
        )
    ):
        if _is_maintenance_quarantined(row):
            continue
        rows.append(
            (
                row,
                _stored_payload(
                    outcome_from_payload,
                    row.payload,
                    record_name="intake outcome",
                ),
            )
        )
    return _latest_intake_outcomes_from_rows(session, rows).get(interaction_id)


def latest_intake_outcome_states(
    session: Session,
) -> dict[uuid.UUID, tuple[WellnessEvent, IntakeOutcome]]:
    rows = _active_intake_outcomes(session)
    by_operation_id = {str(outcome.outcome_id): (row, outcome) for row, outcome in rows}
    latest: dict[
        uuid.UUID,
        tuple[WellnessEvent, IntakeOutcome],
    ] = {}
    transition_interactions: set[uuid.UUID] = set()
    for interaction_id, transition in latest_interaction_transitions(
        session,
        mutation_kind="outcome",
    ).items():
        transition_interactions.add(interaction_id)
        operation_id = transition.payload["operation_id"]
        state = by_operation_id.get(operation_id)
        if state is None or state[1].interaction_id != interaction_id:
            raise NutritionStorageIntegrityError("latest intake outcome result is unavailable")
        if transition.payload.get("mutation_status") != state[1].status.value:
            raise NutritionStorageIntegrityError(
                "interaction transition status does not match the intake outcome payload"
            )
        latest[interaction_id] = state
    legacy = _latest_intake_outcomes_from_rows(session, rows)
    latest.update(
        {
            interaction_id: state
            for interaction_id, state in legacy.items()
            if interaction_id not in transition_interactions
        }
    )
    return latest


def intake_outcome_states_for_day(
    session: Session,
    *,
    start: datetime,
    end: datetime,
) -> list[tuple[WellnessEvent, IntakeOutcome]]:
    states, _unavailable = intake_outcome_ledger_for_day(
        session,
        start=start,
        end=end,
    )
    return states


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
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    latest: dict[uuid.UUID, CaffeineConfirmation] = {}
    for row in rows:
        if _is_maintenance_quarantined(row):
            continue
        confirmation = _stored_payload(
            caffeine_confirmation_from_payload,
            row.payload,
            record_name="caffeine confirmation",
        )
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
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    latest: dict[uuid.UUID, NutritionReview] = {}
    for row in rows:
        if _is_maintenance_quarantined(row):
            continue
        review = _stored_payload(
            nutrition_review_from_payload,
            row.payload,
            record_name="nutrition review",
        )
        if review.observation_id in observation_ids and review.observation_id not in latest:
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
                WellnessEvent.expires_at > datetime.now(UTC),
            ),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
    )
    for row in rows:
        if _is_maintenance_quarantined(row):
            continue
        confirmation = _stored_payload(
            daily_confirmation_from_payload,
            row.payload,
            record_name="daily intake confirmation",
        )
        if confirmation.local_date == local_date and confirmation.timezone == timezone:
            return confirmation
    return None


def local_day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)
