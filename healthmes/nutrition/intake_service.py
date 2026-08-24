"""Nutrition interaction orchestration over the existing wellness event store."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from pathlib import Path
from threading import RLock
from typing import Any

from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from healthmes import clock
from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.config import Settings
from healthmes.nutrition.contracts import (
    ConfirmationStatus,
    Estimate,
    EstimateKind,
    NutritionObservation,
    NutritionReview,
)
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    DecisionScope,
    DecisionStatus,
    EvidenceOrigin,
    IntakeAnalysisProvenance,
    IntakeDecision,
    IntakeDecisionRequest,
    IntakeIntent,
    IntakeInteraction,
    IntakeOutcome,
    IntakeOutcomeStatus,
    NormalizedIntakeItem,
    NutrientFact,
    StructuredIntakeSnapshot,
    decision_from_payload,
    decision_request_from_payload,
    decision_request_to_payload,
    decision_to_payload,
    interaction_from_payload,
    interaction_to_payload,
    outcome_from_payload,
    outcome_to_payload,
)
from healthmes.nutrition.repository import (
    get_observation,
    latest_nutrition_reviews,
    storage_object_for_media,
)
from healthmes.nutrition.schema import (
    SCHEMA_VERSION,
    TEXT_PROMPT_VERSION,
    VLMExtraction,
)
from healthmes.nutrition.transcription import NutritionTranscriber
from healthmes.nutrition.vision import VisionProvider
from healthmes.storage import classify_storage_object, ensure_default_policies
from healthmes.storage.service import DEFAULT_RETENTION, RETENTION_PRESETS
from healthmes.store import RetentionPolicy, WellnessEvent

INTERACTION_EVENT = "nutrition.interaction.v1"
RAW_CAPTURE_EVENT = "nutrition.raw-capture.v1"
OPERATION_EVENT = "nutrition.operation.v1"
OUTCOME_EVENT = "nutrition.intake-outcome.v1"
DECISION_REQUEST_EVENT = "nutrition.decision-request.v1"
DECISION_EVENT = "nutrition.decision.v1"
OUTCOME_RAW_EVENT = "nutrition.outcome-raw.v1"

HIGH_RISK_SCOPES = frozenset(
    {DecisionScope.ALLERGY_SAFETY, DecisionScope.MEDICATION_INTERACTION}
)
MAX_SOURCE_TEXT_CHARS = 12_000
MAX_DECISION_SUMMARY_CHARS = 8_000
MAX_RECOMMENDATION_BYTES = 64 * 1024
MAX_OPERATION_FINGERPRINT_CHARS = 64
MAX_CAPTURE_CLOCK_SKEW = timedelta(minutes=5)
HIGH_RISK_SUMMARY = (
    "The generic HealthMes wellness engine does not provide allergy or "
    "medication-interaction safety decisions."
)
HIGH_RISK_LIMITATIONS = (
    "A separately validated medical-safety policy and qualified clinical "
    "guidance are required for this scope.",
)
CAFFEINE_GENERIC_SUMMARY = (
    "The generic HealthMes nutrition engine cannot make an actionable "
    "caffeine recommendation."
)
CAFFEINE_GENERIC_LIMITATIONS = (
    "A specialized validated caffeine policy must verify confirmed daily "
    "caffeine completeness and the required sleep and safety context.",
)


class IntakeInteractionError(RuntimeError):
    pass


class IntakeOperationConflict(IntakeInteractionError):
    pass


class IntakeAnalysisInProgress(IntakeOperationConflict):
    pass


_ANALYSIS_RESERVATIONS = "nutrition_analysis_reservations"
_STATIC_ANALYSIS_RESERVATIONS: dict[
    tuple[int, uuid.UUID],
    dict[str, Any],
] = {}
_STATIC_ANALYSIS_RESERVATIONS_LOCK = RLock()


def _tracked_analysis_reservations(
    session: Session,
) -> dict[uuid.UUID, str]:
    return session.info.setdefault(_ANALYSIS_RESERVATIONS, {})


def _uses_process_local_reservations(session: Session) -> bool:
    return session.get_bind().dialect.name == "sqlite"


def _pop_static_analysis_reservation(
    session: Session,
    *,
    interaction_id: uuid.UUID,
    reservation_token: str,
    restore_marker: bool = False,
) -> None:
    key = (id(session.get_bind()), interaction_id)
    with _STATIC_ANALYSIS_RESERVATIONS_LOCK:
        reservation = _STATIC_ANALYSIS_RESERVATIONS.get(key)
        if (
            reservation is None
            or reservation["reservation_token"] != reservation_token
        ):
            return
        _STATIC_ANALYSIS_RESERVATIONS.pop(key, None)
    if restore_marker:
        marker = reservation.get("marker")
        prior_payload = reservation.get("prior_payload")
        if marker is not None and prior_payload is not None:
            marker.payload = prior_payload


def _claim_process_local_persistence(
    session: Session,
    *,
    interaction: IntakeInteraction,
    reservation_token: str,
) -> None:
    key = (id(session.get_bind()), interaction.interaction_id)
    now = clock.utc_now()
    with _STATIC_ANALYSIS_RESERVATIONS_LOCK:
        reservation = _STATIC_ANALYSIS_RESERVATIONS.get(key)
        if (
            reservation is None
            or reservation["operation_fingerprint"]
            != interaction.operation_fingerprint
            or reservation["reservation_token"] != reservation_token
            or reservation["lease_expires_at"] <= now
        ):
            raise IntakeAnalysisInProgress(
                "intake interaction analysis reservation is stale"
            )
        reservation["state"] = "persisting"


@event.listens_for(Session, "after_commit")
def _clear_committed_analysis_reservations(session: Session) -> None:
    if session.in_nested_transaction():
        return
    reservations = session.info.pop(_ANALYSIS_RESERVATIONS, {})
    if _uses_process_local_reservations(session):
        for interaction_id, reservation_token in reservations.items():
            _pop_static_analysis_reservation(
                session,
                interaction_id=interaction_id,
                reservation_token=reservation_token,
            )


@event.listens_for(Session, "after_soft_rollback")
def _release_rolled_back_analysis_reservations(
    session: Session,
    previous_transaction: Any,
) -> None:
    if previous_transaction.nested:
        return
    reservations = session.info.pop(_ANALYSIS_RESERVATIONS, {})
    if _uses_process_local_reservations(session):
        for interaction_id, reservation_token in reservations.items():
            _pop_static_analysis_reservation(
                session,
                interaction_id=interaction_id,
                reservation_token=reservation_token,
            )
        return
    for interaction_id, reservation_token in reservations.items():
            _release_interaction_analysis(
                session,
                interaction_id=interaction_id,
                reservation_token=reservation_token,
            )


@event.listens_for(Session, "after_transaction_end")
def _release_closed_analysis_reservations(
    session: Session,
    transaction: Any,
) -> None:
    if transaction.parent is not None or session.in_transaction():
        return
    reservations = session.info.pop(_ANALYSIS_RESERVATIONS, {})
    if _uses_process_local_reservations(session):
        for interaction_id, reservation_token in reservations.items():
            _pop_static_analysis_reservation(
                session,
                interaction_id=interaction_id,
                reservation_token=reservation_token,
            )
        return
    for interaction_id, reservation_token in reservations.items():
        _release_interaction_analysis(
            session,
            interaction_id=interaction_id,
            reservation_token=reservation_token,
        )


def operation_fingerprint(value: dict[str, Any]) -> str:
    """Return a stable digest for caller-controlled idempotency input."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise IntakeInteractionError("timestamps must include a UTC offset")
    return value.astimezone(UTC)


def _stored_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _policy(session: Session, data_class: str) -> RetentionPolicy:
    policy = session.scalar(
        select(RetentionPolicy)
        .where(RetentionPolicy.data_class == data_class)
        .execution_options(populate_existing=True)
    )
    if policy is None:  # pragma: no cover - defaults own this invariant
        raise IntakeInteractionError(f"missing retention policy: {data_class}")
    return policy


def _read_retention_expiry(
    session: Session,
    data_class: str,
    observed_at: datetime,
) -> datetime | None:
    """Read retention without bootstrapping or mutating a search session."""
    policy = session.scalar(
        select(RetentionPolicy)
        .where(RetentionPolicy.data_class == data_class)
        .execution_options(populate_existing=True)
    )
    if policy is not None:
        return _expiry(policy, observed_at)
    preset = DEFAULT_RETENTION.get(data_class)
    if preset is None:
        return None
    days = RETENTION_PRESETS[preset]
    return (
        None
        if days is None
        else observed_at + timedelta(days=days)
    )


def _expiry(policy: RetentionPolicy, observed_at: datetime) -> datetime | None:
    if not policy.enabled or policy.retention_days is None:
        return None
    return observed_at + timedelta(days=policy.retention_days)


def _event_by_source_record(
    session: Session, source_provider: str, source_record_id: str
) -> WellnessEvent | None:
    return session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.source_provider == source_provider,
            WellnessEvent.source_record_id == source_record_id,
        )
        .execution_options(populate_existing=True)
    )


def _validate_operation_fingerprint(value: str) -> None:
    if len(value) != MAX_OPERATION_FINGERPRINT_CHARS or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise IntakeInteractionError(
            "operation_fingerprint must be a lowercase SHA-256 digest"
        )


def _idempotent_existing(
    event: WellnessEvent | None,
    *,
    operation_fingerprint: str,
    operation_name: str,
) -> WellnessEvent | None:
    if event is None:
        return None
    if event.payload.get("operation_fingerprint") != operation_fingerprint:
        raise IntakeOperationConflict(
            f"{operation_name} operation_id was already used with different input"
        )
    return event


def _reject_expired_idempotent(
    event: WellnessEvent,
    *,
    operation_name: str,
) -> None:
    if (
        event.expires_at is not None
        and _stored_as_utc(event.expires_at) <= clock.utc_now()
    ):
        raise IntakeOperationConflict(
            f"expired {operation_name} cannot be retried"
        )


def _persist_event_idempotently(
    session: Session,
    event: WellnessEvent,
    *,
    source_provider: str,
    source_record_id: str,
    operation_fingerprint: str,
    operation_name: str,
) -> WellnessEvent:
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        existing = _event_by_source_record(
            session, source_provider, source_record_id
        )
        if existing is None:
            raise
        return _idempotent_existing(
            existing,
            operation_fingerprint=operation_fingerprint,
            operation_name=operation_name,
        )
    return event


def _interaction_operation_record_id(interaction_id: uuid.UUID) -> str:
    return f"interaction:{interaction_id}"


def _operation_marker(
    *,
    interaction_id: uuid.UUID,
    operation_fingerprint: str,
    recorded_at: datetime,
    state: str,
    reservation_token: str | None = None,
) -> WellnessEvent:
    payload = {
        "operation_kind": "intake_interaction",
        "operation_id": str(interaction_id),
        "operation_fingerprint": operation_fingerprint,
        "operation_state": state,
    }
    if reservation_token is not None:
        payload["reservation_token"] = reservation_token
    return WellnessEvent(
        event_type=OPERATION_EVENT,
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone=None,
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=_interaction_operation_record_id(interaction_id),
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload=payload,
        derived_from=None,
    )


def _persist_interaction_operation_marker(
    session: Session,
    interaction: IntakeInteraction,
    *,
    reservation_token: str | None = None,
) -> WellnessEvent:
    record_id = _interaction_operation_record_id(interaction.interaction_id)
    existing = _idempotent_existing(
        _event_by_source_record(session, "nutrition-operation", record_id),
        operation_fingerprint=interaction.operation_fingerprint,
        operation_name="intake interaction",
    )
    if existing is not None:
        state = existing.payload.get("operation_state")
        if state == "processing":
            if existing.payload.get("reservation_token") != reservation_token:
                raise IntakeAnalysisInProgress(
                    "intake interaction analysis reservation is stale"
                )
            completed_payload = {
                "operation_kind": "intake_interaction",
                "operation_id": str(interaction.interaction_id),
                "operation_fingerprint": interaction.operation_fingerprint,
                "operation_state": "completed",
            }
            if _uses_process_local_reservations(session):
                existing.payload = completed_payload
                session.flush()
            else:
                completed = session.execute(
                    update(WellnessEvent)
                    .where(
                        WellnessEvent.id == existing.id,
                        WellnessEvent.payload[
                            "operation_state"
                        ].as_string()
                        == "processing",
                        WellnessEvent.payload[
                            "reservation_token"
                        ].as_string()
                        == reservation_token,
                    )
                    .values(payload=completed_payload)
                )
                if completed.rowcount != 1:
                    raise IntakeAnalysisInProgress(
                        "intake interaction analysis reservation is stale"
                    )
                session.expire(existing)
        return existing
    recorded_at = _as_utc(interaction.recorded_at)
    marker = _operation_marker(
        interaction_id=interaction.interaction_id,
        operation_fingerprint=interaction.operation_fingerprint,
        recorded_at=recorded_at,
        state="completed",
    )
    return _persist_event_idempotently(
        session,
        marker,
        source_provider="nutrition-operation",
        source_record_id=record_id,
        operation_fingerprint=interaction.operation_fingerprint,
        operation_name="intake interaction",
    )


def _reserve_interaction_analysis(
    session: Session,
    *,
    interaction_id: uuid.UUID,
    operation_fingerprint: str,
    lease_seconds: float,
) -> str:
    reservation_token = uuid.uuid4().hex
    now = clock.utc_now()
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    bind = session.get_bind()
    if _uses_process_local_reservations(session):
        key = (id(bind), interaction_id)
        with _STATIC_ANALYSIS_RESERVATIONS_LOCK:
            active = _STATIC_ANALYSIS_RESERVATIONS.get(key)
            if active is not None:
                if active["operation_fingerprint"] != operation_fingerprint:
                    raise IntakeOperationConflict(
                        "intake interaction operation_id was already used "
                        "with different input"
                    )
                if (
                    active.get("state") == "persisting"
                    or active["lease_expires_at"] > now
                ):
                    raise IntakeAnalysisInProgress(
                        "intake interaction analysis is already in progress"
                    )
            with session.no_autoflush:
                marker = session.scalar(
                    select(WellnessEvent).where(
                        WellnessEvent.source_provider
                        == "nutrition-operation",
                        WellnessEvent.source_record_id
                        == _interaction_operation_record_id(
                            interaction_id
                        ),
                    )
                )
            marker = _idempotent_existing(
                marker,
                operation_fingerprint=operation_fingerprint,
                operation_name="intake interaction",
            )
            prior_payload = None
            if marker is not None:
                if marker.payload.get("operation_state") != "processing":
                    raise IntakeOperationConflict(
                        "intake interaction operation_id belongs to an "
                        "expired capture and cannot be reused"
                    )
                raw_expiry = marker.payload.get("lease_expires_at")
                try:
                    current_expiry = datetime.fromisoformat(raw_expiry)
                except (TypeError, ValueError):
                    current_expiry = datetime.min.replace(tzinfo=UTC)
                if _as_utc(current_expiry) > now:
                    raise IntakeAnalysisInProgress(
                        "intake interaction analysis is already in progress"
                    )
                prior_payload = dict(marker.payload)
                marker.payload = {
                    **marker.payload,
                    "reservation_token": reservation_token,
                    "lease_expires_at": lease_expires_at.isoformat(),
                }
            _STATIC_ANALYSIS_RESERVATIONS[key] = {
                "operation_fingerprint": operation_fingerprint,
                "reservation_token": reservation_token,
                "lease_expires_at": lease_expires_at,
                "state": "reserved",
                "marker": marker,
                "prior_payload": prior_payload,
            }
        return reservation_token

    with activity_write_lock(), Session(bind=bind) as reservation_session:
        lock_activity_write_plane(reservation_session)
        marker = reservation_session.scalar(
            select(WellnessEvent)
            .where(
                WellnessEvent.source_provider == "nutrition-operation",
                WellnessEvent.source_record_id
                == _interaction_operation_record_id(interaction_id),
            )
            .with_for_update()
        )
        marker = _idempotent_existing(
            marker,
            operation_fingerprint=operation_fingerprint,
            operation_name="intake interaction",
        )
        if marker is not None:
            if marker.payload.get("operation_state") != "processing":
                raise IntakeOperationConflict(
                    "intake interaction operation_id belongs to an expired "
                    "capture and cannot be reused"
                )
            raw_expiry = marker.payload.get("lease_expires_at")
            try:
                current_expiry = datetime.fromisoformat(raw_expiry)
            except (TypeError, ValueError):
                current_expiry = datetime.min.replace(tzinfo=UTC)
            if _as_utc(current_expiry) > now:
                raise IntakeAnalysisInProgress(
                    "intake interaction analysis is already in progress"
                )
            prior_token = marker.payload.get("reservation_token")
            replacement = {
                **marker.payload,
                "reservation_token": reservation_token,
                "lease_expires_at": lease_expires_at.isoformat(),
            }
            claimed = reservation_session.execute(
                update(WellnessEvent)
                .where(
                    WellnessEvent.id == marker.id,
                    WellnessEvent.payload[
                        "operation_state"
                    ].as_string()
                    == "processing",
                    WellnessEvent.payload[
                        "reservation_token"
                    ].as_string()
                    == prior_token,
                    WellnessEvent.payload[
                        "lease_expires_at"
                    ].as_string()
                    == raw_expiry,
                )
                .values(payload=replacement)
            )
            if claimed.rowcount != 1:
                reservation_session.rollback()
                raise IntakeAnalysisInProgress(
                    "intake interaction analysis is already in progress"
                )
        else:
            marker = _operation_marker(
                interaction_id=interaction_id,
                operation_fingerprint=operation_fingerprint,
                recorded_at=now,
                state="processing",
                reservation_token=reservation_token,
            )
            marker.payload = {
                **marker.payload,
                "lease_expires_at": lease_expires_at.isoformat(),
            }
            reservation_session.add(marker)
        try:
            reservation_session.commit()
        except IntegrityError as exc:
            reservation_session.rollback()
            raise IntakeAnalysisInProgress(
                "intake interaction analysis is already in progress"
            ) from exc
    return reservation_token


def _release_interaction_analysis(
    session: Session,
    *,
    interaction_id: uuid.UUID,
    reservation_token: str,
) -> None:
    if _uses_process_local_reservations(session):
        _pop_static_analysis_reservation(
            session,
            interaction_id=interaction_id,
            reservation_token=reservation_token,
            restore_marker=True,
        )
        return
    bind = session.get_bind()
    with activity_write_lock(), Session(bind=bind) as reservation_session:
        lock_activity_write_plane(reservation_session)
        marker = reservation_session.scalar(
            select(WellnessEvent)
            .where(
                WellnessEvent.source_provider == "nutrition-operation",
                WellnessEvent.source_record_id
                == _interaction_operation_record_id(interaction_id),
            )
            .with_for_update()
        )
        if (
            marker is not None
            and marker.payload.get("operation_state") == "processing"
            and marker.payload.get("reservation_token") == reservation_token
        ):
            reservation_session.delete(marker)
            reservation_session.commit()


def _validate_estimate(estimate: Estimate) -> None:
    if not estimate.unit.strip() or len(estimate.unit) > 32:
        raise IntakeInteractionError(
            "estimate unit must contain between 1 and 32 characters"
        )
    if estimate.evidence_text is not None and len(estimate.evidence_text) > 500:
        raise IntakeInteractionError(
            "estimate evidence_text cannot exceed 500 characters"
        )
    if (
        estimate.estimation_basis is not None
        and len(estimate.estimation_basis) > 64
    ):
        raise IntakeInteractionError(
            "estimate estimation_basis cannot exceed 64 characters"
        )
    numbers = (estimate.exact, estimate.minimum, estimate.maximum)
    for number in numbers:
        if number is not None and (not isfinite(number) or number < 0):
            raise IntakeInteractionError(
                "nutrition estimates must be finite and non-negative"
            )
    if estimate.kind is EstimateKind.EXACT:
        if (
            estimate.exact is None
            or estimate.minimum is not None
            or estimate.maximum is not None
        ):
            raise IntakeInteractionError("exact estimates require only exact")
    elif estimate.kind is EstimateKind.RANGE:
        if (
            estimate.exact is not None
            or estimate.minimum is None
            or estimate.maximum is None
            or estimate.minimum > estimate.maximum
        ):
            raise IntakeInteractionError("range estimates require ordered bounds")
    elif any(number is not None for number in numbers):
        raise IntakeInteractionError(
            "unknown estimates cannot contain numeric values"
        )


def _validate_items(items: tuple[NormalizedIntakeItem, ...]) -> None:
    if len(items) > 50:
        raise IntakeInteractionError("at most 50 intake items are accepted")
    for item in items:
        if not item.name.strip() or len(item.name) > 300:
            raise IntakeInteractionError(
                "intake item names must contain between 1 and 300 characters"
            )
        if not item.intake_type.strip() or len(item.intake_type) > 32:
            raise IntakeInteractionError(
                "intake_type must contain between 1 and 32 characters"
            )
        if len(item.nutrients) > 100:
            raise IntakeInteractionError(
                "at most 100 nutrient facts are accepted per item"
            )
        if len(item.warnings) > 20 or any(
            len(warning) > 500 for warning in item.warnings
        ):
            raise IntakeInteractionError(
                "at most 20 item warnings of 500 characters are accepted"
            )
        _validate_estimate(item.serving)
        nutrient_keys: set[str] = set()
        for fact in item.nutrients:
            key = fact.nutrient.strip().lower()
            if not key or len(key) > 64:
                raise IntakeInteractionError(
                    "nutrient keys must contain between 1 and 64 characters"
                )
            if key in nutrient_keys:
                raise IntakeInteractionError(
                    f"duplicate nutrient key for one item: {key}"
                )
            nutrient_keys.add(key)
            if fact.evidence_text is not None and len(fact.evidence_text) > 500:
                raise IntakeInteractionError(
                    "nutrient evidence_text cannot exceed 500 characters"
                )
            _validate_estimate(fact.amount)


def normalize_photo_observation(
    observation: NutritionObservation,
) -> tuple[NormalizedIntakeItem, ...]:
    """Adapt the sake observation without changing its stored payload."""

    return normalize_extraction_items(
        observation.items,
        origin=EvidenceOrigin.VLM,
    )


def normalize_extraction_items(
    items: tuple[Any, ...] | list[Any],
    *,
    origin: EvidenceOrigin,
) -> tuple[NormalizedIntakeItem, ...]:
    normalized: list[NormalizedIntakeItem] = []
    for item in items:
        nutrients = tuple(
            NutrientFact(
                nutrient=nutrient.nutrient,
                amount=nutrient.amount,
                confidence=nutrient.confidence,
                origin=origin,
                evidence_text=nutrient.amount.evidence_text,
            )
            for nutrient in item.nutrients
        )
        if (
            not any(value.nutrient == "caffeine" for value in nutrients)
            and item.caffeine.kind is not EstimateKind.UNKNOWN
        ):
            nutrients = (
                *nutrients,
                NutrientFact(
                    nutrient="caffeine",
                    amount=item.caffeine,
                    confidence=item.confidence,
                    origin=origin,
                    evidence_text=item.caffeine.evidence_text,
                )
            )
        normalized.append(
            NormalizedIntakeItem(
                name=(
                    item.name_candidates[0]
                    if item.name_candidates
                    else item.category or "unknown intake"
                ),
                intake_type=item.intake_type.value,
                serving=item.serving,
                nutrients=nutrients,
                confidence=item.confidence,
                warnings=item.warnings,
            )
        )
    return tuple(normalized)


def _safe_media_path(settings: Settings, media_path: str) -> Path:
    data_root = settings.data_dir.resolve()
    candidate = (settings.data_dir / media_path).resolve()
    media_root = (settings.data_dir / "media").resolve()
    if (
        data_root not in candidate.parents
        or media_root not in candidate.parents
        or not candidate.is_file()
    ):
        raise IntakeInteractionError("media storage object is unavailable")
    return candidate


def create_analyzed_interaction(
    session: Session,
    settings: Settings,
    *,
    operation_id: uuid.UUID,
    operation_fingerprint: str,
    intent: IntakeIntent,
    modality: CaptureModality,
    observed_at: datetime,
    timezone: str,
    source: str,
    source_text: str | None,
    media_path: str | None,
    recorded_at: datetime,
    allow_remote_analysis: bool,
    provider: VisionProvider,
    transcriber: NutritionTranscriber | None = None,
) -> IntakeInteraction:
    """Analyze free text or local voice, then persist one immutable interaction."""

    if modality not in {CaptureModality.TEXT, CaptureModality.VOICE}:
        raise IntakeInteractionError(
            "automatic capture analysis supports text or voice"
        )
    with session.no_autoflush:
        existing = _existing_interaction_operation(
            session,
            interaction_id=operation_id,
            operation_fingerprint=operation_fingerprint,
            allow_processing=True,
        )
    if existing is not None:
        return existing
    reservation_token = _reserve_interaction_analysis(
        session,
        interaction_id=operation_id,
        operation_fingerprint=operation_fingerprint,
        lease_seconds=(
            settings.nutrition_vision_timeout_seconds
            + (
                settings.nutrition_transcription_timeout_seconds
                if modality is CaptureModality.VOICE
                else 0
            )
            + 120
        ),
    )
    _tracked_analysis_reservations(session)[operation_id] = (
        reservation_token
    )
    persistence_started = False

    try:
        transcript_provider = None
        transcript_model = None
        analyzed_text = source_text
        if modality is CaptureModality.TEXT:
            if media_path is not None:
                raise IntakeInteractionError(
                    "text analysis cannot reference media"
                )
            if not analyzed_text or not analyzed_text.strip():
                raise IntakeInteractionError(
                    "text analysis requires source_text"
                )
        else:
            if media_path is None:
                raise IntakeInteractionError(
                    "voice analysis requires media_path"
                )
            obj = storage_object_for_media(session, media_path)
            if obj is None:
                raise IntakeInteractionError("media storage object not found")
            if not (obj.content_type or "").startswith("audio/"):
                raise IntakeInteractionError(
                    "voice analysis requires an audio storage object"
                )
            if transcriber is None:
                raise IntakeInteractionError(
                    "voice analysis requires a configured local transcriber"
                )
            transcript = transcriber.transcribe(
                _safe_media_path(settings, media_path)
            )
            analyzed_text = transcript.text
            transcript_provider = transcript.provider
            transcript_model = transcript.model

        assert analyzed_text is not None
        extraction: VLMExtraction = provider.analyze_text(
            analyzed_text,
            allow_remote=allow_remote_analysis,
        )
        interaction = IntakeInteraction(
            interaction_id=operation_id,
            operation_fingerprint=operation_fingerprint,
            intent=intent,
            modality=modality,
            observed_at=observed_at,
            recorded_at=recorded_at,
            timezone=timezone,
            source=source,
            source_text=analyzed_text,
            media_path=media_path,
            nutrition_observation_id=None,
            items=normalize_extraction_items(
                tuple(item.to_domain() for item in extraction.items),
                origin=EvidenceOrigin.AGENT,
            ),
            analysis_provenance=IntakeAnalysisProvenance(
                provider=provider.provider_name,
                model=provider.model,
                model_digest=provider.model_digest,
                prompt_version=TEXT_PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
                analyzed_at=recorded_at,
                transcription_provider=transcript_provider,
                transcription_model=transcript_model,
            ),
            warnings=tuple(extraction.warnings),
        )
        persistence_started = True
        create_interaction(
            session,
            settings,
            interaction,
            reservation_token=reservation_token,
        )
        return interaction
    except Exception:
        if not persistence_started:
            _tracked_analysis_reservations(session).pop(
                operation_id,
                None,
            )
            _release_interaction_analysis(
                session,
                interaction_id=operation_id,
                reservation_token=reservation_token,
            )
        raise


def normalize_nutrition_review(
    observation: NutritionObservation,
    review: NutritionReview | None,
) -> tuple[NormalizedIntakeItem, ...]:
    if review is None:
        return normalize_photo_observation(observation)
    if review.status is ConfirmationStatus.CONFIRMED:
        return normalize_extraction_items(
            observation.items,
            origin=EvidenceOrigin.USER,
        )
    if review.status is ConfirmationStatus.REJECTED:
        raise IntakeInteractionError(
            "rejected nutrition observations cannot create interactions"
        )
    return tuple(
        NormalizedIntakeItem(
            name=item.name,
            intake_type=item.intake_type.value,
            serving=item.serving,
            nutrients=tuple(
                NutrientFact(
                    nutrient=nutrient.nutrient,
                    amount=nutrient.amount,
                    confidence=nutrient.confidence,
                    origin=EvidenceOrigin.USER,
                    evidence_text=None,
                )
                for nutrient in item.nutrients
            ),
            confidence=item.confidence,
            warnings=item.warnings,
        )
        for item in sorted(review.items, key=lambda value: value.item_index)
    )


def structured_snapshot(
    interaction: IntakeInteraction,
    *,
    items: tuple[NormalizedIntakeItem, ...] | None = None,
) -> StructuredIntakeSnapshot:
    durable_items = tuple(
        replace(
            item,
            serving=replace(item.serving, evidence_text=None),
            nutrients=tuple(
                replace(
                    fact,
                    amount=replace(fact.amount, evidence_text=None),
                    evidence_text=None,
                )
                for fact in item.nutrients
            ),
            warnings=(),
        )
        for item in (interaction.items if items is None else items)
    )
    return StructuredIntakeSnapshot(
        interaction_id=interaction.interaction_id,
        intent=interaction.intent,
        modality=interaction.modality,
        observed_at=interaction.observed_at,
        timezone=interaction.timezone,
        source=interaction.source,
        nutrition_observation_id=interaction.nutrition_observation_id,
        items=durable_items,
        nutrition_review_id=interaction.nutrition_review_id,
        analysis_provenance=interaction.analysis_provenance,
        warnings=(),
    )


def _without_raw_evidence(
    interaction: IntakeInteraction,
) -> IntakeInteraction:
    return replace(
        interaction,
        source_text=None,
        media_path=None,
        items=tuple(
            replace(
                item,
                serving=replace(item.serving, evidence_text=None),
                nutrients=tuple(
                    replace(
                        fact,
                        amount=replace(
                            fact.amount,
                            evidence_text=None,
                        ),
                        evidence_text=None,
                    )
                    for fact in item.nutrients
                ),
                warnings=(),
            )
            for item in interaction.items
        ),
        warnings=(),
    )


def create_interaction(
    session: Session,
    settings: Settings,
    interaction: IntakeInteraction,
    *,
    reservation_token: str | None = None,
) -> WellnessEvent:
    lock_activity_write_plane(session)
    ensure_default_policies(session)
    _validate_operation_fingerprint(interaction.operation_fingerprint)
    if (
        reservation_token is not None
        and _uses_process_local_reservations(session)
    ):
        _claim_process_local_persistence(
            session,
            interaction=interaction,
            reservation_token=reservation_token,
        )
    marker = _idempotent_existing(
        _event_by_source_record(
            session,
            "nutrition-operation",
            _interaction_operation_record_id(interaction.interaction_id),
        ),
        operation_fingerprint=interaction.operation_fingerprint,
        operation_name="intake interaction",
    )
    existing = _idempotent_existing(
        _event_by_source_record(
            session, "nutrition-interaction", str(interaction.interaction_id)
        ),
        operation_fingerprint=interaction.operation_fingerprint,
        operation_name="intake interaction",
    )
    if existing is not None:
        _reject_expired_idempotent(
            existing,
            operation_name="intake interaction",
        )
        if marker is None:
            _persist_interaction_operation_marker(session, interaction)
        return existing
    if marker is not None:
        if marker.payload.get("operation_state") != "processing":
            raise IntakeOperationConflict(
                "intake interaction operation_id belongs to an expired "
                "capture and cannot be reused"
            )
        if marker.payload.get("reservation_token") != reservation_token:
            raise IntakeAnalysisInProgress(
                "intake interaction analysis is already in progress"
            )

    _validate_items(interaction.items)
    if not interaction.source.strip() or len(interaction.source) > 64:
        raise IntakeInteractionError(
            "source must contain between 1 and 64 characters"
        )
    if not interaction.timezone.strip() or len(interaction.timezone) > 64:
        raise IntakeInteractionError(
            "timezone must contain between 1 and 64 characters"
        )
    if (
        interaction.source_text is not None
        and len(interaction.source_text) > MAX_SOURCE_TEXT_CHARS
    ):
        raise IntakeInteractionError(
            f"source_text cannot exceed {MAX_SOURCE_TEXT_CHARS} characters"
        )
    if interaction.media_path is not None and len(interaction.media_path) > 500:
        raise IntakeInteractionError("media_path cannot exceed 500 characters")
    if len(interaction.warnings) > 20 or any(
        len(warning) > 500 for warning in interaction.warnings
    ):
        raise IntakeInteractionError(
            "at most 20 interaction warnings of 500 characters are accepted"
        )
    observed_at = _as_utc(interaction.observed_at)
    recorded_at = _as_utc(interaction.recorded_at)
    if observed_at > clock.utc_now() + MAX_CAPTURE_CLOCK_SKEW:
        raise IntakeInteractionError(
            "observed_at cannot be more than 5 minutes in the future"
        )
    raw_object_id = None

    if interaction.modality is CaptureModality.PHOTO:
        if interaction.nutrition_observation_id is None:
            raise IntakeInteractionError(
                "photo interactions require nutrition_observation_id"
            )
        observation = get_observation(
            session, interaction.nutrition_observation_id
        )
        if observation is None:
            raise IntakeInteractionError("nutrition observation not found")
        if interaction.media_path != observation.capture.media_path:
            raise IntakeInteractionError(
                "photo media_path must match the nutrition observation"
            )
        review = latest_nutrition_reviews(
            session, {observation.observation_id}
        ).get(observation.observation_id)
        if interaction.nutrition_review_id != (
            review.review_id if review is not None else None
        ):
            raise IntakeInteractionError(
                "photo interaction must reference the latest nutrition review"
            )
        if interaction.items != normalize_nutrition_review(observation, review):
            raise IntakeInteractionError(
                "photo items must match the latest reviewed nutrition observation"
            )
    elif interaction.nutrition_observation_id is not None:
        raise IntakeInteractionError(
            "text and voice interactions cannot reference a photo observation"
        )
    elif interaction.nutrition_review_id is not None:
        raise IntakeInteractionError(
            "text and voice interactions cannot reference a nutrition review"
        )

    if interaction.modality is CaptureModality.TEXT:
        if not interaction.source_text or not interaction.source_text.strip():
            raise IntakeInteractionError("text interactions require source_text")
        if interaction.media_path is not None:
            raise IntakeInteractionError("text interactions cannot reference media")

    if interaction.modality is CaptureModality.VOICE:
        if not interaction.source_text or not interaction.source_text.strip():
            raise IntakeInteractionError(
                "voice interactions require a local transcript"
            )
        if interaction.media_path is None:
            raise IntakeInteractionError("voice interactions require media_path")

    if interaction.media_path is not None:
        obj = storage_object_for_media(session, interaction.media_path)
        if obj is None:
            raise IntakeInteractionError("media storage object not found")
        existing_media_owner = session.scalar(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.source_provider == "nutrition-raw-capture",
                WellnessEvent.raw_object_id == obj.id,
                WellnessEvent.source_record_id
                != str(interaction.interaction_id),
            )
        )
        if existing_media_owner is not None:
            raise IntakeInteractionError(
                "media storage object already belongs to another capture"
            )
        expected_prefix = (
            "image/"
            if interaction.modality is CaptureModality.PHOTO
            else "audio/"
        )
        if not (obj.content_type or "").startswith(expected_prefix):
            raise IntakeInteractionError(
                f"{interaction.modality.value} interaction has incompatible media"
            )
        classify_storage_object(
            session,
            obj,
            data_class="nutrition_media",
            observed_at=observed_at,
            safe_to_purge=True,
        )
        raw_object_id = obj.id

    policy = _policy(session, "nutrition_observation")
    structured_expiry = _expiry(policy, observed_at)
    if (
        structured_expiry is not None
        and structured_expiry <= clock.utc_now()
    ):
        raise IntakeInteractionError(
            "observed_at falls outside the structured retention window"
        )
    durable_interaction = _without_raw_evidence(interaction)
    event = WellnessEvent(
        event_type=INTERACTION_EVENT,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=recorded_at,
        timezone=interaction.timezone,
        source_provider="nutrition-interaction",
        source_device=interaction.source,
        source_record_id=str(interaction.interaction_id),
        capture_method=interaction.modality.value,
        quality_flags={"warning_count": len(interaction.warnings)},
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=structured_expiry,
        payload=interaction_to_payload(durable_interaction),
        raw_object_id=None,
        derived_from=(
            {
                "nutrition_observation_id": str(
                    interaction.nutrition_observation_id
                ),
                **(
                    {
                        "nutrition_review_id": str(
                            interaction.nutrition_review_id
                        )
                    }
                    if interaction.nutrition_review_id is not None
                    else {}
                ),
            }
            if interaction.nutrition_observation_id is not None
            else None
        ),
    )
    stored = _persist_event_idempotently(
        session,
        event,
        source_provider="nutrition-interaction",
        source_record_id=str(interaction.interaction_id),
        operation_fingerprint=interaction.operation_fingerprint,
        operation_name="intake interaction",
    )
    raw_policy = _policy(session, "nutrition_raw_capture")
    raw_event = WellnessEvent(
        event_type=RAW_CAPTURE_EVENT,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=recorded_at,
        timezone=interaction.timezone,
        source_provider="nutrition-raw-capture",
        source_device=interaction.source,
        source_record_id=str(interaction.interaction_id),
        capture_method=interaction.modality.value,
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=raw_policy.id,
        expires_at=_expiry(raw_policy, observed_at),
        payload={
            "operation_fingerprint": interaction.operation_fingerprint,
            "source_text": interaction.source_text,
            "media_path": interaction.media_path,
            "warnings": list(interaction.warnings),
            "item_warnings": [
                list(item.warnings) for item in interaction.items
            ],
        },
        raw_object_id=raw_object_id,
        derived_from={"interaction_id": str(interaction.interaction_id)},
    )
    try:
        _persist_event_idempotently(
            session,
            raw_event,
            source_provider="nutrition-raw-capture",
            source_record_id=str(interaction.interaction_id),
            operation_fingerprint=interaction.operation_fingerprint,
            operation_name="intake raw capture",
        )
    except IntegrityError as exc:
        raise IntakeInteractionError(
            "media storage object already belongs to another capture"
        ) from exc
    _persist_interaction_operation_marker(
        session,
        interaction,
        reservation_token=reservation_token,
    )
    return stored


def get_interaction(
    session: Session, interaction_id: uuid.UUID
) -> IntakeInteraction | None:
    event = _event_by_source_record(
        session, "nutrition-interaction", str(interaction_id)
    )
    if (
        event is None
        or (
            event.expires_at is not None
            and _stored_as_utc(event.expires_at) <= clock.utc_now()
        )
    ):
        return None
    interaction = interaction_from_payload(event.payload)
    raw = _event_by_source_record(
        session,
        "nutrition-raw-capture",
        str(interaction_id),
    )
    raw_is_available = (
        raw is not None
        and (
            raw.expires_at is None
            or _stored_as_utc(raw.expires_at) > clock.utc_now()
        )
    )
    source_text = interaction.source_text
    media_path = interaction.media_path
    if raw_is_available:
        assert raw is not None
        raw_source_text = raw.payload.get("source_text")
        raw_media_path = raw.payload.get("media_path")
        raw_warnings = raw.payload.get("warnings")
        raw_item_warnings = raw.payload.get("item_warnings")
        source_text = (
            raw_source_text if isinstance(raw_source_text, str) else None
        )
        media_path = (
            raw_media_path if isinstance(raw_media_path, str) else None
        )
        interaction = replace(
            interaction,
            warnings=(
                tuple(
                    warning
                    for warning in raw_warnings
                    if isinstance(warning, str)
                )
                if isinstance(raw_warnings, list)
                else ()
            ),
            items=tuple(
                replace(
                    item,
                    warnings=(
                        tuple(
                            warning
                            for warning in raw_item_warnings[index]
                            if isinstance(warning, str)
                        )
                        if (
                            isinstance(raw_item_warnings, list)
                            and index < len(raw_item_warnings)
                            and isinstance(
                                raw_item_warnings[index],
                                list,
                            )
                        )
                        else ()
                    ),
                )
                for index, item in enumerate(interaction.items)
            ),
        )
    else:
        raw_expiry = _read_retention_expiry(
            session,
            "nutrition_raw_capture",
            _as_utc(interaction.observed_at),
        )
        if (
            raw is not None
            or (
                raw_expiry is not None
                and raw_expiry <= clock.utc_now()
            )
        ):
            source_text = None
            media_path = None
            interaction = _without_raw_evidence(interaction)
    if media_path is not None:
        obj = storage_object_for_media(session, media_path)
        if obj is None or obj.purged_at is not None:
            media_path = None
    return replace(
        interaction,
        source_text=source_text if isinstance(source_text, str) else None,
        media_path=media_path if isinstance(media_path, str) else None,
    )


def _existing_interaction_operation(
    session: Session,
    *,
    interaction_id: uuid.UUID,
    operation_fingerprint: str,
    allow_processing: bool = False,
) -> IntakeInteraction | None:
    _validate_operation_fingerprint(operation_fingerprint)
    marker = _idempotent_existing(
        _event_by_source_record(
            session,
            "nutrition-operation",
            _interaction_operation_record_id(interaction_id),
        ),
        operation_fingerprint=operation_fingerprint,
        operation_name="intake interaction",
    )
    existing = _idempotent_existing(
        _event_by_source_record(
            session, "nutrition-interaction", str(interaction_id)
        ),
        operation_fingerprint=operation_fingerprint,
        operation_name="intake interaction",
    )
    if existing is not None:
        stored = get_interaction(session, interaction_id)
        if stored is None:
            _reject_expired_idempotent(
                existing,
                operation_name="intake interaction",
            )
        return stored
    if marker is not None:
        if marker.payload.get("operation_state") == "processing":
            if allow_processing:
                session.expire(marker)
                return None
            raise IntakeAnalysisInProgress(
                "intake interaction analysis is already in progress"
            )
        raise IntakeOperationConflict(
            "intake interaction operation_id belongs to an expired capture "
            "and cannot be reused"
        )
    return None


def create_photo_interaction(
    session: Session,
    settings: Settings,
    *,
    observation_id: uuid.UUID,
    operation_id: uuid.UUID,
    operation_fingerprint: str,
    intent: IntakeIntent,
    source: str,
    recorded_at: datetime,
    source_text: str | None = None,
) -> IntakeInteraction:
    # Read the observation and its latest review under the same retention
    # generation that will be used by create_interaction(). Model analysis is
    # performed by the caller before this function, so this fence covers only
    # the short database read/write section.
    lock_activity_write_plane(session)
    existing = _existing_interaction_operation(
        session,
        interaction_id=operation_id,
        operation_fingerprint=operation_fingerprint,
    )
    if existing is not None:
        return existing
    observation = get_observation(session, observation_id)
    if observation is None:
        raise IntakeInteractionError("nutrition observation not found")
    review = latest_nutrition_reviews(session, {observation_id}).get(
        observation_id
    )
    interaction = IntakeInteraction(
        interaction_id=operation_id,
        operation_fingerprint=operation_fingerprint,
        intent=intent,
        modality=CaptureModality.PHOTO,
        observed_at=observation.capture.captured_at,
        recorded_at=recorded_at,
        timezone=observation.capture.timezone,
        source=source,
        source_text=source_text,
        media_path=observation.capture.media_path,
        nutrition_observation_id=observation.observation_id,
        items=normalize_nutrition_review(observation, review),
        nutrition_review_id=review.review_id if review is not None else None,
        analysis_provenance=IntakeAnalysisProvenance(
            provider=observation.vision.provider,
            model=observation.vision.model,
            model_digest=observation.vision.model_digest,
            prompt_version=observation.vision.prompt_version,
            schema_version=observation.vision.schema_version,
            analyzed_at=observation.vision.analyzed_at,
        ),
        warnings=observation.warnings,
    )
    create_interaction(session, settings, interaction)
    return interaction


def persist_outcome(session: Session, outcome: IntakeOutcome) -> WellnessEvent:
    lock_activity_write_plane(session)
    ensure_default_policies(session)
    _validate_operation_fingerprint(outcome.operation_fingerprint)
    existing = _idempotent_existing(
        _event_by_source_record(
            session, "nutrition-intake-outcome", str(outcome.outcome_id)
        ),
        operation_fingerprint=outcome.operation_fingerprint,
        operation_name="intake outcome",
    )
    if existing is not None:
        _reject_expired_idempotent(
            existing,
            operation_name="intake outcome",
        )
        return existing
    interaction = get_interaction(session, outcome.interaction_id)
    if interaction is None:
        raise IntakeInteractionError("intake interaction not found")
    if not outcome.source.strip() or len(outcome.source) > 64:
        raise IntakeInteractionError(
            "source must contain between 1 and 64 characters"
        )
    if outcome.note is not None and len(outcome.note) > 2000:
        raise IntakeInteractionError("outcome note cannot exceed 2000 characters")
    if outcome.status is IntakeOutcomeStatus.CONSUMED:
        if outcome.consumed_at is None:
            raise IntakeInteractionError(
                "consumed outcomes require consumed_at"
            )
        consumed_at = _as_utc(outcome.consumed_at)
        if consumed_at > _as_utc(outcome.confirmed_at) + timedelta(minutes=5):
            raise IntakeInteractionError(
                "consumed_at cannot be in the future"
            )
    elif outcome.consumed_at is not None:
        raise IntakeInteractionError(
            "non-consumed outcomes cannot include consumed_at"
        )
    _validate_items(outcome.corrected_items)

    confirmed_at = _as_utc(outcome.confirmed_at)
    durable_outcome = replace(
        outcome,
        corrected_items=structured_snapshot(
            interaction,
            items=outcome.corrected_items,
        ).items,
        note=None,
        intake_snapshot=structured_snapshot(
            interaction,
            items=(
                outcome.corrected_items
                if outcome.corrected_items
                else interaction.items
            ),
        ),
    )
    policy = _policy(session, "nutrition_confirmation")
    event = WellnessEvent(
        event_type=OUTCOME_EVENT,
        schema_version=1,
        observed_at=_as_utc(outcome.consumed_at or outcome.confirmed_at),
        recorded_at=confirmed_at,
        timezone=interaction.timezone,
        source_provider="nutrition-intake-outcome",
        source_device=outcome.source,
        source_record_id=str(outcome.outcome_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=_expiry(policy, confirmed_at),
        payload=outcome_to_payload(durable_outcome),
        derived_from={"interaction_id": str(outcome.interaction_id)},
    )
    stored = _persist_event_idempotently(
        session,
        event,
        source_provider="nutrition-intake-outcome",
        source_record_id=str(outcome.outcome_id),
        operation_fingerprint=outcome.operation_fingerprint,
        operation_name="intake outcome",
    )
    if outcome.note is not None:
        raw_policy = _policy(session, "nutrition_raw_capture")
        raw_event = WellnessEvent(
            event_type=OUTCOME_RAW_EVENT,
            schema_version=1,
            observed_at=confirmed_at,
            recorded_at=confirmed_at,
            timezone=interaction.timezone,
            source_provider="nutrition-outcome-raw",
            source_device=outcome.source,
            source_record_id=str(outcome.outcome_id),
            capture_method="manual",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=raw_policy.id,
            expires_at=_expiry(raw_policy, confirmed_at),
            payload={
                "operation_fingerprint": outcome.operation_fingerprint,
                "note": outcome.note,
            },
            derived_from={"outcome_id": str(outcome.outcome_id)},
        )
        _persist_event_idempotently(
            session,
            raw_event,
            source_provider="nutrition-outcome-raw",
            source_record_id=str(outcome.outcome_id),
            operation_fingerprint=outcome.operation_fingerprint,
            operation_name="intake outcome raw note",
        )
    return stored


def latest_outcome(
    session: Session, interaction_id: uuid.UUID
) -> tuple[WellnessEvent, IntakeOutcome] | None:
    rows = session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == OUTCOME_EVENT,
            (
                WellnessEvent.expires_at.is_(None)
                | (WellnessEvent.expires_at > clock.utc_now())
            ),
            WellnessEvent.payload["interaction_id"].as_string()
            == str(interaction_id),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
        .limit(1)
    )
    for row in rows:
        outcome = outcome_from_payload(row.payload)
        if outcome.interaction_id == interaction_id:
            raw = _event_by_source_record(
                session,
                "nutrition-outcome-raw",
                str(outcome.outcome_id),
            )
            if (
                raw is not None
                and (
                    raw.expires_at is None
                    or _stored_as_utc(raw.expires_at) > clock.utc_now()
                )
                and isinstance(raw.payload.get("note"), str)
            ):
                outcome = replace(outcome, note=raw.payload["note"])
            return row, outcome
    return None


def list_interactions(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    intent: IntakeIntent | None = None,
    modality: CaptureModality | None = None,
    limit: int | None = 100,
) -> list[tuple[WellnessEvent, IntakeInteraction]]:
    statement = (
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == INTERACTION_EVENT,
            (
                WellnessEvent.expires_at.is_(None)
                | (WellnessEvent.expires_at > clock.utc_now())
            ),
        )
        .order_by(WellnessEvent.observed_at.desc(), WellnessEvent.created_at.desc())
    )
    if start is not None:
        statement = statement.where(WellnessEvent.observed_at >= _as_utc(start))
    if end is not None:
        statement = statement.where(WellnessEvent.observed_at < _as_utc(end))
    if intent is not None:
        statement = statement.where(
            WellnessEvent.payload["intent"].as_string() == intent.value
        )
    if modality is not None:
        statement = statement.where(
            WellnessEvent.payload["modality"].as_string() == modality.value
        )
    if limit is not None:
        statement = statement.limit(limit)
    return [
        (row, interaction_from_payload(row.payload))
        for row in session.scalars(statement)
    ]


def persist_decision_request(
    session: Session, request: IntakeDecisionRequest
) -> WellnessEvent:
    lock_activity_write_plane(session)
    ensure_default_policies(session)
    _validate_operation_fingerprint(request.operation_fingerprint)
    existing = _idempotent_existing(
        _event_by_source_record(
            session, "nutrition-decision-request", str(request.request_id)
        ),
        operation_fingerprint=request.operation_fingerprint,
        operation_name="intake decision request",
    )
    if existing is not None:
        _reject_expired_idempotent(
            existing,
            operation_name="intake decision request",
        )
        return existing
    interaction = get_interaction(session, request.interaction_id)
    if interaction is None:
        raise IntakeInteractionError("intake interaction not found")
    if interaction.intent is IntakeIntent.INSPECT_ONLY:
        raise IntakeInteractionError(
            "inspect-only interactions cannot request a wellness decision"
        )
    if not request.source.strip() or len(request.source) > 64:
        raise IntakeInteractionError(
            "source must contain between 1 and 64 characters"
        )
    if request.question is not None and len(request.question) > 2000:
        raise IntakeInteractionError(
            "decision question cannot exceed 2000 characters"
        )
    if not 1 <= request.lookback_days <= 90:
        raise IntakeInteractionError(
            "lookback_days must be between 1 and 90"
        )
    compare_ids = set(request.compare_interaction_ids)
    if request.interaction_id in compare_ids:
        raise IntakeInteractionError(
            "compare_interaction_ids cannot repeat the primary interaction"
        )
    if len(compare_ids) != len(request.compare_interaction_ids):
        raise IntakeInteractionError(
            "compare_interaction_ids must not contain duplicates"
        )
    if len(compare_ids) > 20:
        raise IntakeInteractionError(
            "at most 20 comparison interactions are accepted"
        )
    for interaction_id in compare_ids:
        if get_interaction(session, interaction_id) is None:
            raise IntakeInteractionError(
                f"comparison interaction not found: {interaction_id}"
            )
    if request.scope is DecisionScope.COMPARE_OPTIONS and not compare_ids:
        raise IntakeInteractionError(
            "compare_options requires at least one comparison interaction"
        )
    if request.intended_consumption_at is not None:
        _as_utc(request.intended_consumption_at)

    requested_at = _as_utc(request.requested_at)
    policy = _policy(session, "decision")
    event_id = uuid.uuid4()
    from healthmes.nutrition.intake_query import build_decision_context_snapshot

    context_snapshot = build_decision_context_snapshot(
        session,
        request=request,
        request_event_id=event_id,
    )
    durable_request = replace(request, context_snapshot=context_snapshot)
    event = WellnessEvent(
        id=event_id,
        event_type=DECISION_REQUEST_EVENT,
        schema_version=1,
        observed_at=requested_at,
        recorded_at=requested_at,
        timezone=interaction.timezone,
        source_provider="nutrition-decision-request",
        source_device=request.source,
        source_record_id=str(request.request_id),
        capture_method="manual",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=_expiry(policy, requested_at),
        payload=decision_request_to_payload(durable_request),
        derived_from={
            "interaction_id": str(request.interaction_id),
            "compare_interaction_ids": [
                str(value) for value in request.compare_interaction_ids
            ],
        },
    )
    return _persist_event_idempotently(
        session,
        event,
        source_provider="nutrition-decision-request",
        source_record_id=str(request.request_id),
        operation_fingerprint=request.operation_fingerprint,
        operation_name="intake decision request",
    )


def get_decision_request(
    session: Session, request_id: uuid.UUID
) -> tuple[WellnessEvent, IntakeDecisionRequest] | None:
    event = _event_by_source_record(
        session, "nutrition-decision-request", str(request_id)
    )
    if (
        event is None
        or (
            event.expires_at is not None
            and _stored_as_utc(event.expires_at) <= clock.utc_now()
        )
    ):
        return None
    return event, decision_request_from_payload(event.payload)


def persist_decision(session: Session, decision: IntakeDecision) -> WellnessEvent:
    lock_activity_write_plane(session)
    ensure_default_policies(session)
    _validate_operation_fingerprint(decision.operation_fingerprint)
    existing = _idempotent_existing(
        _event_by_source_record(
            session, "nutrition-decision", str(decision.decision_id)
        ),
        operation_fingerprint=decision.operation_fingerprint,
        operation_name="intake decision",
    )
    if existing is not None:
        _reject_expired_idempotent(
            existing,
            operation_name="intake decision",
        )
        return existing
    request_entry = get_decision_request(session, decision.request_id)
    if request_entry is None:
        raise IntakeInteractionError("intake decision request not found")
    request_event, request = request_entry
    if decision.interaction_id != request.interaction_id:
        raise IntakeInteractionError(
            "decision interaction does not match its request"
        )
    if decision.scope is not request.scope:
        raise IntakeInteractionError("decision scope does not match its request")
    if decision.scope in HIGH_RISK_SCOPES and (
        decision.status is not DecisionStatus.UNSUPPORTED
    ):
        raise IntakeInteractionError(
            "high-risk nutrition scopes must remain unsupported"
        )
    if decision.scope is DecisionScope.CAFFEINE_SLEEP and decision.status in {
        DecisionStatus.PROPOSAL,
        DecisionStatus.NOOP,
    }:
        raise IntakeInteractionError(
            "generic caffeine decisions cannot store proposals or no-op results; "
            "the specialized caffeine policy must own actionable decisions"
        )
    if not decision.summary.strip():
        raise IntakeInteractionError("decision summary must not be empty")
    if len(decision.summary) > MAX_DECISION_SUMMARY_CHARS:
        raise IntakeInteractionError(
            f"decision summary cannot exceed {MAX_DECISION_SUMMARY_CHARS} characters"
        )
    if not decision.source.strip() or len(decision.source) > 64:
        raise IntakeInteractionError(
            "source must contain between 1 and 64 characters"
        )
    if len(decision.limitations) > 50 or any(
        len(value) > 1000 for value in decision.limitations
    ):
        raise IntakeInteractionError(
            "at most 50 decision limitations of 1000 characters are accepted"
        )
    if (
        decision.recommendation is not None
        and decision.scope not in HIGH_RISK_SCOPES
        and decision.scope is not DecisionScope.CAFFEINE_SLEEP
    ):
        try:
            recommendation_size = len(
                json.dumps(
                    decision.recommendation,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise IntakeInteractionError(
                "decision recommendation must be JSON serializable"
            ) from exc
        if recommendation_size > MAX_RECOMMENDATION_BYTES:
            raise IntakeInteractionError(
                f"decision recommendation cannot exceed {MAX_RECOMMENDATION_BYTES} bytes"
            )
    if (
        decision.status is DecisionStatus.UNSUPPORTED
        and decision.recommendation is not None
        and decision.scope not in HIGH_RISK_SCOPES
        and decision.scope is not DecisionScope.CAFFEINE_SLEEP
    ):
        raise IntakeInteractionError(
            "unsupported decisions cannot contain a recommendation"
        )
    durable_decision = decision
    if decision.scope in HIGH_RISK_SCOPES:
        durable_decision = replace(
            decision,
            summary=HIGH_RISK_SUMMARY,
            limitations=HIGH_RISK_LIMITATIONS,
            recommendation=None,
        )
    elif decision.scope is DecisionScope.CAFFEINE_SLEEP:
        durable_decision = replace(
            decision,
            summary=CAFFEINE_GENERIC_SUMMARY,
            limitations=CAFFEINE_GENERIC_LIMITATIONS,
            recommendation=None,
        )
    evidence_ids = set(decision.evidence_event_ids)
    if len(evidence_ids) != len(decision.evidence_event_ids):
        raise IntakeInteractionError(
            "decision evidence_event_ids must not contain duplicates"
        )
    if request_event.id not in evidence_ids:
        raise IntakeInteractionError(
            "decision evidence must include the decision request event"
        )
    if len(evidence_ids) > 500:
        raise IntakeInteractionError(
            "at most 500 decision evidence events are accepted"
        )
    from healthmes.nutrition.intake_query import decision_context

    context = decision_context(session, request_id=request.request_id)
    if context is None:  # pragma: no cover - request was loaded above
        raise IntakeInteractionError("intake decision context not found")
    allowed_evidence = {
        uuid.UUID(value) for value in context["evidence_event_ids"]
    }
    if not evidence_ids.issubset(allowed_evidence):
        raise IntakeInteractionError(
            "decision evidence must come from the stored decision context"
        )

    decided_at = _as_utc(decision.decided_at)
    policy = _policy(session, "decision")
    event = WellnessEvent(
        event_type=DECISION_EVENT,
        schema_version=1,
        observed_at=decided_at,
        recorded_at=decided_at,
        timezone=None,
        source_provider="nutrition-decision",
        source_device=decision.source,
        source_record_id=str(decision.decision_id),
        capture_method="agent",
        quality_flags={"limitations": list(durable_decision.limitations)},
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=_expiry(policy, decided_at),
        payload=decision_to_payload(durable_decision),
        derived_from={
            "request_id": str(decision.request_id),
            "interaction_id": str(decision.interaction_id),
            "evidence_event_ids": [
                str(value) for value in decision.evidence_event_ids
            ],
        },
    )
    return _persist_event_idempotently(
        session,
        event,
        source_provider="nutrition-decision",
        source_record_id=str(decision.decision_id),
        operation_fingerprint=decision.operation_fingerprint,
        operation_name="intake decision",
    )


def latest_decision(
    session: Session, interaction_id: uuid.UUID
) -> tuple[WellnessEvent, IntakeDecision] | None:
    rows = session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == DECISION_EVENT,
            (
                WellnessEvent.expires_at.is_(None)
                | (WellnessEvent.expires_at > clock.utc_now())
            ),
            WellnessEvent.payload["interaction_id"].as_string()
            == str(interaction_id),
        )
        .order_by(WellnessEvent.recorded_at.desc(), WellnessEvent.created_at.desc())
        .limit(1)
    )
    for row in rows:
        decision = decision_from_payload(row.payload)
        if decision.interaction_id == interaction_id:
            return row, decision
    return None


def persisted_decision_for_operation(
    session: Session,
    *,
    decision_id: uuid.UUID,
    operation_fingerprint: str,
) -> IntakeDecision | None:
    _validate_operation_fingerprint(operation_fingerprint)
    event = _idempotent_existing(
        _event_by_source_record(
            session, "nutrition-decision", str(decision_id)
        ),
        operation_fingerprint=operation_fingerprint,
        operation_name="intake decision",
    )
    if event is None:
        return None
    _reject_expired_idempotent(
        event,
        operation_name="intake decision",
    )
    return decision_from_payload(event.payload)


def resolved_items(
    interaction: IntakeInteraction, outcome: IntakeOutcome | None
) -> tuple[NormalizedIntakeItem, ...]:
    if outcome is not None and outcome.corrected_items:
        return outcome.corrected_items
    return interaction.items
