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
    NutrientEstimate,
    ObservationStatus,
)

CORE_NUTRIENT_UNITS: dict[str, str] = {
    "energy": "kcal",
    "protein": "g",
    "carbohydrate": "g",
    "fat": "g",
    "fiber": "g",
    "sugar": "g",
    "sodium": "mg",
    "caffeine": "mg",
}
SUPPORTED_NUTRIENT_UNITS = frozenset(
    {"kcal", "kJ", "g", "mg", "mcg", "IU", "ml"}
)


class VLMEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EstimateKind
    unit: str = Field(min_length=1, max_length=32)
    exact: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    minimum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    maximum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
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


class VLMNutrient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nutrient: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    amount: VLMEstimate
    confidence: Confidence

    @model_validator(mode="after")
    def validate_unit(self) -> Self:
        if self.amount.unit not in SUPPORTED_NUTRIENT_UNITS:
            raise ValueError(
                f"unsupported nutrient unit: {self.amount.unit}"
            )
        expected = CORE_NUTRIENT_UNITS.get(self.nutrient)
        if expected is not None and self.amount.unit != expected:
            raise ValueError(
                f"{self.nutrient} estimates must use {expected}"
            )
        return self

    def to_domain(self) -> NutrientEstimate:
        return NutrientEstimate(
            nutrient=self.nutrient,
            amount=self.amount.to_domain(),
            confidence=self.confidence,
        )


class VLMItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intake_type: IntakeType
    name_candidates: list[str] = Field(default_factory=list, max_length=10)
    category: str | None = Field(default=None, max_length=64)
    serving: VLMEstimate
    caffeine: VLMEstimate
    nutrients: list[VLMNutrient] = Field(default_factory=list, max_length=64)
    label_text_candidates: list[str] = Field(default_factory=list, max_length=20)
    product_code_candidates: list[str] = Field(default_factory=list, max_length=10)
    confidence: Confidence
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_nutrients(self) -> Self:
        if self.caffeine.unit != "mg":
            raise ValueError("caffeine estimates must use mg")
        by_name: dict[str, VLMNutrient] = {}
        for nutrient in self.nutrients:
            if nutrient.nutrient in by_name:
                raise ValueError(
                    f"duplicate nutrient estimate: {nutrient.nutrient}"
                )
            by_name[nutrient.nutrient] = nutrient
        caffeine = by_name.get("caffeine")
        if caffeine is not None and caffeine.amount != self.caffeine:
            raise ValueError(
                "caffeine and nutrients[caffeine] must match"
            )
        if caffeine is None:
            self.nutrients.append(
                VLMNutrient(
                    nutrient="caffeine",
                    amount=self.caffeine,
                    confidence=self.confidence,
                )
            )
        for nutrient, unit in CORE_NUTRIENT_UNITS.items():
            if nutrient in {value.nutrient for value in self.nutrients}:
                continue
            self.nutrients.append(
                VLMNutrient(
                    nutrient=nutrient,
                    amount=VLMEstimate(
                        kind=EstimateKind.UNKNOWN,
                        unit=unit,
                    ),
                    confidence=Confidence.LOW,
                )
            )
        return self

    def to_domain(self) -> IntakeItem:
        return IntakeItem(
            intake_type=self.intake_type,
            name_candidates=tuple(self.name_candidates),
            category=self.category,
            serving=self.serving.to_domain(),
            caffeine=self.caffeine.to_domain(),
            nutrients=tuple(
                nutrient.to_domain() for nutrient in self.nutrients
            ),
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
Return only the supplied JSON schema. Split visibly distinct foods and drinks into
separate items. For every item estimate serving and the core nutrients energy,
protein, carbohydrate, fat, fiber, sugar, sodium, and caffeine. Add other visible or
reasonably estimable nutrients when useful. Use canonical lowercase snake_case names
and canonical units: energy kcal; macronutrients g; sodium and caffeine mg.
Exact numbers are allowed only when a readable label shows them. Otherwise use a
bounded range with an explicit visual, portion, recipe, or food-composition basis;
use unknown when the image cannot support a bounded estimate. Never turn unknown
into zero. Never infer capture time, timezone, location, daily completeness,
medication dose, allergens, ingredients that are not visible, or a user's identity.
All values are unconfirmed estimates, not medical advice."""

PROMPT_VERSION = "photo-intake-v2"
SCHEMA_VERSION = "nutrition-observation-v2"
SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/heic", "image/webp"})

# Kept separate so an adapter cannot accidentally accept arbitrary role text.
USER_PROMPT: Literal["Analyze the attached intake photo."] = "Analyze the attached intake photo."
