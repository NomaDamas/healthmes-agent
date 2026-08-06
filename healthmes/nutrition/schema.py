"""Strict schema accepted from a bounded photo VLM."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from healthmes.nutrition.contracts import (
    Confidence,
    Estimate,
    EstimateKind,
    IntakeItem,
    IntakeType,
    ObservationStatus,
)


class VLMEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
            if self.estimation_basis != "visible_label" or not self.evidence_text:
                raise ValueError("exact estimates require visible-label evidence")
        elif self.kind is EstimateKind.RANGE:
            if (
                self.exact is not None
                or self.minimum is None
                or self.maximum is None
                or self.minimum > self.maximum
            ):
                raise ValueError("range estimates require an ordered minimum and maximum")
            if not self.estimation_basis:
                raise ValueError("range estimates require an estimation basis")
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


class VLMItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intake_type: IntakeType
    name_candidates: list[str] = Field(default_factory=list, max_length=10)
    category: str | None = Field(default=None, max_length=64)
    serving: VLMEstimate
    caffeine: VLMEstimate
    label_text_candidates: list[str] = Field(default_factory=list, max_length=20)
    product_code_candidates: list[str] = Field(default_factory=list, max_length=10)
    confidence: Confidence
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_caffeine_unit(self) -> Self:
        if self.caffeine.unit != "mg":
            raise ValueError("caffeine estimates must use mg")
        return self

    def to_domain(self) -> IntakeItem:
        return IntakeItem(
            intake_type=self.intake_type,
            name_candidates=tuple(self.name_candidates),
            category=self.category,
            serving=self.serving.to_domain(),
            caffeine=self.caffeine.to_domain(),
            label_text_candidates=tuple(self.label_text_candidates),
            product_code_candidates=tuple(self.product_code_candidates),
            confidence=self.confidence,
            warnings=tuple(self.warnings),
        )


class VLMExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ObservationStatus
    confidence: Confidence
    warnings: list[str] = Field(default_factory=list, max_length=20)
    items: list[VLMItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status is ObservationStatus.USABLE and not self.items:
            raise ValueError("usable observations require at least one item")
        return self


SYSTEM_PROMPT = """You extract visible food and beverage evidence from one image.
Return only the supplied JSON schema. Never infer capture time, timezone, location,
daily completeness, medication dose, or a user's identity. Exact serving or caffeine
numbers are allowed only when a readable label shows the number. Otherwise use a
bounded range with an explicit basis or unknown. Zero is not a substitute for
unknown. All values are unconfirmed observations, not medical advice."""

PROMPT_VERSION = "photo-intake-v1"
SCHEMA_VERSION = "nutrition-observation-v1"
SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/heic", "image/webp"})

# Kept separate so an adapter cannot accidentally accept arbitrary role text.
USER_PROMPT: Literal["Analyze the attached intake photo."] = "Analyze the attached intake photo."
