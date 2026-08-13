"""Device-neutral REST adapter for the nutrition interaction engine."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Self

from fastapi import APIRouter, Request, status
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from healthmes.api.common import utc_now
from healthmes.api.errors import APIError, not_found
from healthmes.config import Settings
from healthmes.nutrition.contracts import Confidence, Estimate, EstimateKind
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    DecisionScope,
    DecisionStatus,
    EvidenceOrigin,
    IntakeDecision,
    IntakeDecisionRequest,
    IntakeIntent,
    IntakeInteraction,
    IntakeOutcome,
    IntakeOutcomeStatus,
    NormalizedIntakeItem,
    NutrientFact,
)
from healthmes.nutrition.intake_query import (
    decision_context,
    interaction_view,
    search_intake_history,
)
from healthmes.nutrition.intake_service import (
    HIGH_RISK_SCOPES,
    IntakeInteractionError,
    IntakeOperationConflict,
    create_analyzed_interaction,
    create_interaction,
    create_photo_interaction,
    get_decision_request,
    operation_fingerprint,
    persist_decision,
    persist_decision_request,
    persist_outcome,
    persisted_decision_for_operation,
)
from healthmes.nutrition.transcription import (
    NutritionTranscriber,
    TranscriptionInvalidOutput,
    TranscriptionUnavailable,
    create_nutrition_transcriber,
)
from healthmes.nutrition.vision import (
    VisionInvalidOutput,
    VisionProvider,
    VisionUnavailable,
    create_vision_provider,
)
from healthmes.store.session import SessionDep
from healthmes.timezones import parse_timezone

router = APIRouter(prefix="/v1/intake-interactions", tags=["nutrition"])
MAX_CAPTURE_CLOCK_SKEW = timedelta(minutes=5)


class EstimateInput(BaseModel):
    kind: EstimateKind
    unit: str = Field(min_length=1, max_length=32)
    exact: float | None = Field(default=None, ge=0)
    minimum: float | None = Field(default=None, ge=0)
    maximum: float | None = Field(default=None, ge=0)
    evidence_text: str | None = Field(default=None, max_length=500)
    estimation_basis: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.kind is EstimateKind.EXACT:
            if self.exact is None or self.minimum is not None or self.maximum is not None:
                raise ValueError("exact estimates require only exact")
        elif self.kind is EstimateKind.RANGE:
            if (
                self.exact is not None
                or self.minimum is None
                or self.maximum is None
                or self.minimum > self.maximum
            ):
                raise ValueError("range estimates require ordered bounds")
        elif any(value is not None for value in (self.exact, self.minimum, self.maximum)):
            raise ValueError("unknown estimates cannot carry numeric values")
        return self

    def to_domain(self) -> Estimate:
        return Estimate(
            kind=self.kind,
            unit=self.unit,
            exact=self.exact,
            minimum=self.minimum,
            maximum=self.maximum,
            evidence_text=self.evidence_text,
            estimation_basis=self.estimation_basis,
        )


class NutrientFactInput(BaseModel):
    nutrient: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    amount: EstimateInput
    confidence: Confidence
    origin: EvidenceOrigin
    evidence_text: str | None = Field(default=None, max_length=500)

    def to_domain(self) -> NutrientFact:
        return NutrientFact(
            nutrient=self.nutrient,
            amount=self.amount.to_domain(),
            confidence=self.confidence,
            origin=self.origin,
            evidence_text=self.evidence_text,
        )


class IntakeItemInput(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    intake_type: str = Field(min_length=1, max_length=32)
    serving: EstimateInput
    nutrients: list[NutrientFactInput] = Field(default_factory=list, max_length=100)
    confidence: Confidence = Confidence.LOW
    warnings: list[str] = Field(default_factory=list, max_length=20)

    def to_domain(self) -> NormalizedIntakeItem:
        return NormalizedIntakeItem(
            name=self.name,
            intake_type=self.intake_type,
            serving=self.serving.to_domain(),
            nutrients=tuple(value.to_domain() for value in self.nutrients),
            confidence=self.confidence,
            warnings=tuple(self.warnings),
        )


class CreateInteractionInput(BaseModel):
    operation_id: uuid.UUID
    intent: IntakeIntent
    modality: CaptureModality
    observed_at: AwareDatetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)
    source_text: str | None = Field(default=None, max_length=12000)
    media_path: str | None = Field(default=None, max_length=500)
    nutrition_observation_id: uuid.UUID | None = None
    items: list[IntakeItemInput] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_capture(self) -> Self:
        if self.modality is CaptureModality.PHOTO:
            if self.nutrition_observation_id is None:
                raise ValueError("photo requires nutrition_observation_id")
            if self.items:
                raise ValueError("photo items come from the stored sake observation")
            return self
        if self.observed_at is None or self.timezone is None:
            raise ValueError("text and voice require observed_at and timezone")
        try:
            timezone = parse_timezone(self.timezone)
        except ValueError as exc:
            raise ValueError(
                "timezone must be a valid IANA or UTC fixed-offset timezone"
            ) from exc
        if self.observed_at.utcoffset() != self.observed_at.astimezone(timezone).utcoffset():
            raise ValueError("observed_at offset conflicts with timezone")
        if self.observed_at.astimezone(UTC) > datetime.now(UTC) + MAX_CAPTURE_CLOCK_SKEW:
            raise ValueError("observed_at cannot be more than 5 minutes in the future")
        return self


class AnalyzeInteractionInput(BaseModel):
    operation_id: uuid.UUID
    intent: IntakeIntent
    modality: CaptureModality
    observed_at: AwareDatetime
    timezone: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)
    source_text: str | None = Field(default=None, max_length=12000)
    media_path: str | None = Field(default=None, max_length=500)
    allow_remote_analysis: bool = False

    @model_validator(mode="after")
    def validate_capture(self) -> Self:
        if self.modality not in {
            CaptureModality.TEXT,
            CaptureModality.VOICE,
        }:
            raise ValueError(
                "automatic interaction analysis supports text or voice"
            )
        try:
            timezone = parse_timezone(self.timezone)
        except ValueError as exc:
            raise ValueError(
                "timezone must be a valid IANA or UTC fixed-offset timezone"
            ) from exc
        if (
            self.observed_at.utcoffset()
            != self.observed_at.astimezone(timezone).utcoffset()
        ):
            raise ValueError("observed_at offset conflicts with timezone")
        if self.observed_at.astimezone(UTC) > datetime.now(UTC) + MAX_CAPTURE_CLOCK_SKEW:
            raise ValueError("observed_at cannot be more than 5 minutes in the future")
        if self.modality is CaptureModality.TEXT:
            if not self.source_text or not self.source_text.strip():
                raise ValueError("text analysis requires source_text")
            if self.media_path is not None:
                raise ValueError("text analysis cannot reference media")
        if self.modality is CaptureModality.VOICE:
            if self.source_text is not None:
                raise ValueError(
                    "voice analysis creates source_text from local transcription"
                )
            if self.media_path is None:
                raise ValueError("voice analysis requires media_path")
        return self


class OutcomeInput(BaseModel):
    operation_id: uuid.UUID
    status: IntakeOutcomeStatus
    source: str = Field(min_length=1, max_length=64)
    consumed_at: AwareDatetime | None = None
    corrected_items: list[IntakeItemInput] = Field(default_factory=list, max_length=50)
    note: str | None = Field(default=None, max_length=2000)


class DecisionRequestInput(BaseModel):
    operation_id: uuid.UUID
    scope: DecisionScope
    source: str = Field(min_length=1, max_length=64)
    question: str | None = Field(default=None, max_length=2000)
    intended_consumption_at: AwareDatetime | None = None
    compare_interaction_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    lookback_days: int = Field(default=14, ge=1, le=90)


class DecisionInput(BaseModel):
    operation_id: uuid.UUID
    request_id: uuid.UUID
    status: DecisionStatus
    source: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=8000)
    evidence_event_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    limitations: list[str] = Field(default_factory=list, max_length=50)
    recommendation: dict[str, object] | None = None


def _raise_interaction_error(exc: IntakeInteractionError) -> None:
    if isinstance(exc, IntakeOperationConflict):
        raise APIError(
            status.HTTP_409_CONFLICT,
            "operation_id_conflict",
            str(exc),
        ) from exc
    raise APIError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "invalid_intake_interaction",
        str(exc),
    ) from exc


def _analysis_provider(request: Request, settings: Settings) -> VisionProvider:
    override = getattr(
        request.app.state,
        "nutrition_analysis_provider",
        None,
    )
    if override is None:
        override = getattr(
            request.app.state,
            "nutrition_vision_provider",
            None,
        )
    return override if override is not None else create_vision_provider(settings)


def _transcriber(
    request: Request,
    settings: Settings,
) -> NutritionTranscriber:
    override = getattr(request.app.state, "nutrition_transcriber", None)
    return (
        override
        if override is not None
        else create_nutrition_transcriber(settings)
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_intake_interaction(
    body: CreateInteractionInput,
    request: Request,
    session: SessionDep,
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    now = utc_now()
    fingerprint = operation_fingerprint(body.model_dump(mode="json"))
    try:
        if body.modality is CaptureModality.PHOTO:
            interaction = create_photo_interaction(
                session,
                settings,
                observation_id=body.nutrition_observation_id,
                operation_id=body.operation_id,
                operation_fingerprint=fingerprint,
                intent=body.intent,
                source=body.source,
                recorded_at=now,
                source_text=body.source_text,
            )
        else:
            interaction = IntakeInteraction(
                interaction_id=body.operation_id,
                operation_fingerprint=fingerprint,
                intent=body.intent,
                modality=body.modality,
                observed_at=body.observed_at.astimezone(UTC),
                recorded_at=now,
                timezone=body.timezone,
                source=body.source,
                source_text=body.source_text,
                media_path=body.media_path,
                nutrition_observation_id=None,
                items=tuple(item.to_domain() for item in body.items),
            )
            create_interaction(session, settings, interaction)
    except IntakeInteractionError as exc:
        _raise_interaction_error(exc)
    session.commit()
    view = interaction_view(session, interaction.interaction_id)
    assert view is not None
    return view


@router.post("/analyze", status_code=status.HTTP_201_CREATED)
def analyze_intake_interaction(
    body: AnalyzeInteractionInput,
    request: Request,
    session: SessionDep,
) -> dict[str, object]:
    """Automatically structure a free-text or local voice nutrition capture."""

    settings: Settings = request.app.state.settings
    fingerprint = operation_fingerprint(body.model_dump(mode="json"))
    try:
        interaction = create_analyzed_interaction(
            session,
            settings,
            operation_id=body.operation_id,
            operation_fingerprint=fingerprint,
            intent=body.intent,
            modality=body.modality,
            observed_at=body.observed_at.astimezone(UTC),
            timezone=body.timezone,
            source=body.source,
            source_text=body.source_text,
            media_path=body.media_path,
            recorded_at=utc_now(),
            allow_remote_analysis=body.allow_remote_analysis,
            provider=_analysis_provider(request, settings),
            transcriber=(
                _transcriber(request, settings)
                if body.modality is CaptureModality.VOICE
                else None
            ),
        )
    except IntakeInteractionError as exc:
        _raise_interaction_error(exc)
    except TranscriptionUnavailable as exc:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "transcription_unavailable",
            str(exc),
        ) from exc
    except TranscriptionInvalidOutput as exc:
        raise APIError(
            status.HTTP_502_BAD_GATEWAY,
            "transcription_invalid_output",
            str(exc),
        ) from exc
    except VisionUnavailable as exc:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "nutrition_analysis_unavailable",
            str(exc),
        ) from exc
    except VisionInvalidOutput as exc:
        raise APIError(
            status.HTTP_502_BAD_GATEWAY,
            "nutrition_analysis_invalid_output",
            str(exc),
        ) from exc
    session.commit()
    view = interaction_view(session, interaction.interaction_id)
    assert view is not None
    return view


@router.get("")
def search_intake_interactions(
    session: SessionDep,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
    intent: IntakeIntent | None = None,
    modality: CaptureModality | None = None,
    confirmed_only: bool = False,
    nutrient: str | None = None,
    query: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, object]:
    return search_intake_history(
        session,
        start=start,
        end=end,
        intent=intent,
        modality=modality,
        confirmed_only=confirmed_only,
        nutrient=nutrient,
        query=query,
        limit=limit,
    )


@router.get("/{interaction_id}")
def get_intake_interaction(
    interaction_id: uuid.UUID, session: SessionDep
) -> dict[str, object]:
    view = interaction_view(session, interaction_id)
    if view is None:
        raise not_found("intake interaction", interaction_id)
    return view


@router.post("/{interaction_id}/outcomes", status_code=status.HTTP_201_CREATED)
def confirm_intake_outcome(
    interaction_id: uuid.UUID,
    body: OutcomeInput,
    session: SessionDep,
) -> dict[str, object]:
    outcome = IntakeOutcome(
        outcome_id=body.operation_id,
        operation_fingerprint=operation_fingerprint(
            {
                "interaction_id": str(interaction_id),
                **body.model_dump(mode="json"),
            }
        ),
        interaction_id=interaction_id,
        status=body.status,
        confirmed_at=utc_now(),
        source=body.source,
        consumed_at=(
            body.consumed_at.astimezone(UTC)
            if body.consumed_at is not None
            else None
        ),
        corrected_items=tuple(item.to_domain() for item in body.corrected_items),
        note=body.note,
    )
    try:
        persist_outcome(session, outcome)
    except IntakeInteractionError as exc:
        _raise_interaction_error(exc)
    session.commit()
    view = interaction_view(session, interaction_id)
    assert view is not None
    return view


@router.post(
    "/{interaction_id}/decision-requests",
    status_code=status.HTTP_201_CREATED,
)
def create_decision_request(
    interaction_id: uuid.UUID,
    body: DecisionRequestInput,
    session: SessionDep,
) -> dict[str, object]:
    request = IntakeDecisionRequest(
        request_id=body.operation_id,
        operation_fingerprint=operation_fingerprint(
            {
                "interaction_id": str(interaction_id),
                **body.model_dump(mode="json"),
            }
        ),
        interaction_id=interaction_id,
        scope=body.scope,
        requested_at=utc_now(),
        source=body.source,
        question=body.question,
        intended_consumption_at=(
            body.intended_consumption_at.astimezone(UTC)
            if body.intended_consumption_at is not None
            else None
        ),
        compare_interaction_ids=tuple(body.compare_interaction_ids),
        lookback_days=body.lookback_days,
    )
    try:
        persist_decision_request(session, request)
    except IntakeInteractionError as exc:
        _raise_interaction_error(exc)
    session.commit()
    return {
        "request_id": str(request.request_id),
        "interaction_id": str(interaction_id),
        "scope": request.scope.value,
        "requires_specialized_policy": request.scope in HIGH_RISK_SCOPES,
    }


@router.get("/decision-requests/{request_id}/context")
def get_decision_context(
    request_id: uuid.UUID,
    session: SessionDep,
) -> dict[str, object]:
    context = decision_context(session, request_id=request_id)
    if context is None:
        raise not_found("intake decision request", request_id)
    return context


@router.post(
    "/{interaction_id}/decisions",
    status_code=status.HTTP_201_CREATED,
)
def record_intake_decision(
    interaction_id: uuid.UUID,
    body: DecisionInput,
    session: SessionDep,
) -> dict[str, object]:
    fingerprint = operation_fingerprint(
        {
            "interaction_id": str(interaction_id),
            **body.model_dump(mode="json"),
        }
    )
    try:
        existing = persisted_decision_for_operation(
            session,
            decision_id=body.operation_id,
            operation_fingerprint=fingerprint,
        )
    except IntakeInteractionError as exc:
        _raise_interaction_error(exc)
    if existing is not None:
        return {
            "decision_id": str(existing.decision_id),
            "interaction_id": str(existing.interaction_id),
            "scope": existing.scope.value,
            "status": existing.status.value,
        }
    request_entry = get_decision_request(session, body.request_id)
    if request_entry is None:
        raise not_found("intake decision request", body.request_id)
    decision = IntakeDecision(
        decision_id=body.operation_id,
        operation_fingerprint=fingerprint,
        request_id=body.request_id,
        interaction_id=interaction_id,
        scope=request_entry[1].scope,
        status=body.status,
        decided_at=utc_now(),
        source=body.source,
        summary=body.summary,
        evidence_event_ids=tuple(body.evidence_event_ids),
        limitations=tuple(body.limitations),
        recommendation=body.recommendation,
    )
    try:
        persist_decision(session, decision)
    except IntakeInteractionError as exc:
        _raise_interaction_error(exc)
    session.commit()
    return {
        "decision_id": str(decision.decision_id),
        "interaction_id": str(interaction_id),
        "scope": decision.scope.value,
        "status": decision.status.value,
    }
