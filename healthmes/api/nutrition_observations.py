"""Photo analysis and immutable confirmation APIs for sake observations."""

from __future__ import annotations

import uuid
from datetime import UTC, date
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request, status
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from healthmes.api.common import utc_now
from healthmes.api.errors import APIError, not_found
from healthmes.api.media import resolve_media_file
from healthmes.config import Settings
from healthmes.nutrition.contracts import (
    CaffeineConfirmation,
    CaptureContext,
    ConfirmationStatus,
    ConfirmedCaffeineItem,
    DailyIntakeConfirmation,
    Location,
    MetadataSource,
    NutritionObservation,
    VisionProvenance,
)
from healthmes.nutrition.repository import (
    NutritionRepositoryError,
    get_observation,
    list_observations,
    observation_for_media,
    persist_caffeine_confirmation,
    persist_daily_confirmation,
    persist_observation,
    storage_object_for_media,
)
from healthmes.nutrition.schema import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_IMAGE_TYPES,
)
from healthmes.nutrition.vision import (
    VisionInvalidOutput,
    VisionProvider,
    VisionUnavailable,
    create_vision_provider,
)
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/nutrition-observations", tags=["nutrition"])


class LocationInput(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: float | None = Field(default=None, ge=0)


class AnalyzeNutritionPhoto(BaseModel):
    media_path: str = Field(
        pattern=r"^media/[A-Za-z0-9_.\-/]+$",
        max_length=500,
    )
    captured_at: AwareDatetime
    timezone: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)
    location: LocationInput | None = None
    metadata_provenance: dict[str, MetadataSource]
    allow_remote_vision: bool = False

    @model_validator(mode="after")
    def validate_capture_context(self) -> Self:
        try:
            timezone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        expected_offset = self.captured_at.astimezone(timezone).utcoffset()
        if self.captured_at.utcoffset() != expected_offset:
            raise ValueError("captured_at offset conflicts with timezone")
        required = {"captured_at", "timezone", "location"}
        if not required.issubset(self.metadata_provenance):
            raise ValueError(
                "metadata_provenance requires captured_at, timezone, and location"
            )
        if (
            self.location is None
            and self.metadata_provenance["location"] is not MetadataSource.UNAVAILABLE
        ):
            raise ValueError("missing location must use unavailable provenance")
        return self


class ConfirmedCaffeineInput(BaseModel):
    item_index: int = Field(ge=0)
    caffeine_mg: float = Field(ge=0)


class ConfirmObservationInput(BaseModel):
    status: Literal["confirmed", "corrected", "rejected"]
    source: str = Field(min_length=1, max_length=64)
    items: list[ConfirmedCaffeineInput] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_items(self) -> Self:
        if self.status == "rejected" and self.items:
            raise ValueError("rejected observations cannot contain confirmed items")
        if self.status != "rejected" and not self.items:
            raise ValueError("confirmed or corrected observations require item values")
        indexes = [item.item_index for item in self.items]
        if len(indexes) != len(set(indexes)):
            raise ValueError("item_index values must be unique")
        return self


class ConfirmDayInput(BaseModel):
    local_date: date
    timezone: str = Field(min_length=1, max_length=64)
    observation_ids: list[uuid.UUID] = Field(max_length=100)
    total_intake_complete: bool
    source: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_timezone_and_ids(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("observation_ids must not contain duplicates")
        return self


def _provider(request: Request, settings: Settings) -> VisionProvider:
    override = getattr(request.app.state, "nutrition_vision_provider", None)
    return override if override is not None else create_vision_provider(settings)


@router.post(
    "/analyze",
    status_code=status.HTTP_201_CREATED,
    response_model=NutritionObservation,
)
def analyze_nutrition_photo(
    body: AnalyzeNutritionPhoto,
    request: Request,
    session: SessionDep,
) -> NutritionObservation:
    settings: Settings = request.app.state.settings
    image_path = resolve_media_file(settings, body.media_path)
    if image_path is None:
        raise not_found("media", body.media_path)
    obj = storage_object_for_media(session, body.media_path)
    if obj is None:
        raise not_found("storage object", body.media_path)
    if obj.content_type not in SUPPORTED_IMAGE_TYPES:
        raise APIError(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            "nutrition analysis accepts image media only",
        )
    existing = observation_for_media(session, obj.id)
    if existing is not None:
        return existing

    provider = _provider(request, settings)
    try:
        extraction = provider.analyze(
            image_path,
            allow_remote=body.allow_remote_vision,
        )
    except VisionUnavailable as exc:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "vision_unavailable",
            str(exc),
        ) from exc
    except VisionInvalidOutput as exc:
        raise APIError(
            status.HTTP_502_BAD_GATEWAY,
            "vision_invalid_output",
            str(exc),
        ) from exc

    analyzed_at = utc_now()
    observation = NutritionObservation(
        observation_id=uuid.uuid4(),
        capture=CaptureContext(
            media_path=body.media_path,
            captured_at=body.captured_at.astimezone(UTC),
            timezone=body.timezone,
            source=body.source,
            location=(
                Location(
                    latitude=body.location.latitude,
                    longitude=body.location.longitude,
                    accuracy_meters=body.location.accuracy_meters,
                )
                if body.location is not None
                else None
            ),
            metadata_provenance=dict(body.metadata_provenance),
        ),
        status=extraction.status,
        confidence=extraction.confidence,
        warnings=tuple(extraction.warnings),
        items=tuple(item.to_domain() for item in extraction.items),
        vision=VisionProvenance(
            provider=provider.provider_name,
            model=provider.model,
            model_digest=provider.model_digest,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            analyzed_at=analyzed_at,
        ),
    )
    try:
        persist_observation(session, settings, observation)
    except NutritionRepositoryError as exc:
        raise APIError(
            status.HTTP_409_CONFLICT,
            "nutrition_storage_conflict",
            str(exc),
        ) from exc
    session.commit()
    return observation


@router.get("", response_model=list[NutritionObservation])
def get_nutrition_observations(
    session: SessionDep,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> list[NutritionObservation]:
    return list_observations(session, start=start, end=end, limit=limit)


@router.post("/daily-confirmations", status_code=status.HTTP_201_CREATED)
def confirm_daily_intake(
    body: ConfirmDayInput, session: SessionDep
) -> dict[str, object]:
    confirmation = DailyIntakeConfirmation(
        confirmation_id=uuid.uuid4(),
        local_date=body.local_date,
        timezone=body.timezone,
        observation_ids=tuple(body.observation_ids),
        total_intake_complete=body.total_intake_complete,
        confirmed_at=utc_now(),
        source=body.source,
    )
    try:
        persist_daily_confirmation(session, confirmation)
    except NutritionRepositoryError as exc:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_daily_nutrition_confirmation",
            str(exc),
        ) from exc
    session.commit()
    return {
        "confirmation_id": str(confirmation.confirmation_id),
        "local_date": confirmation.local_date.isoformat(),
        "total_intake_complete": confirmation.total_intake_complete,
    }


@router.get("/{observation_id}", response_model=NutritionObservation)
def get_nutrition_observation(
    observation_id: uuid.UUID, session: SessionDep
) -> NutritionObservation:
    observation = get_observation(session, observation_id)
    if observation is None:
        raise not_found("nutrition observation", observation_id)
    return observation


@router.post("/{observation_id}/confirm", status_code=status.HTTP_201_CREATED)
def confirm_nutrition_observation(
    observation_id: uuid.UUID,
    body: ConfirmObservationInput,
    session: SessionDep,
) -> dict[str, str]:
    confirmation = CaffeineConfirmation(
        confirmation_id=uuid.uuid4(),
        observation_id=observation_id,
        status=ConfirmationStatus(body.status),
        confirmed_at=utc_now(),
        source=body.source,
        items=tuple(
            ConfirmedCaffeineItem(
                item_index=item.item_index,
                caffeine_mg=item.caffeine_mg,
            )
            for item in body.items
        ),
    )
    try:
        persist_caffeine_confirmation(session, confirmation)
    except NutritionRepositoryError as exc:
        if str(exc) == "nutrition observation not found":
            raise not_found("nutrition observation", observation_id) from exc
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_nutrition_confirmation",
            str(exc),
        ) from exc
    session.commit()
    return {
        "confirmation_id": str(confirmation.confirmation_id),
        "observation_id": str(observation_id),
        "status": confirmation.status.value,
    }
