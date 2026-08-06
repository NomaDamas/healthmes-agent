"""Read-only nutrition evidence views for MCP and decision skills."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from healthmes.nutrition.contracts import (
    ConfirmationStatus,
    EstimateKind,
    NutritionObservation,
    NutritionReview,
    caffeine_confirmation_to_payload,
    nutrition_review_to_payload,
    observation_to_payload,
)
from healthmes.nutrition.repository import (
    latest_caffeine_confirmations,
    latest_daily_confirmation,
    latest_nutrition_reviews,
    list_observations,
    local_day_bounds,
)


def observation_view(
    observation: NutritionObservation,
    caffeine_confirmation: Any | None,
    nutrition_review: Any | None = None,
) -> dict[str, Any]:
    payload = observation_to_payload(observation)
    payload["latest_confirmation"] = (
        caffeine_confirmation_to_payload(caffeine_confirmation)
        if caffeine_confirmation is not None
        else None
    )
    payload["latest_nutrition_review"] = (
        nutrition_review_to_payload(nutrition_review)
        if nutrition_review is not None
        else None
    )
    return payload


def nutrition_observations_view(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    observations = list_observations(session, start=start, end=end, limit=limit)
    ids = {observation.observation_id for observation in observations}
    confirmations = latest_caffeine_confirmations(session, ids)
    reviews = latest_nutrition_reviews(session, ids)
    return {
        "status": "ok",
        "count": len(observations),
        "observations": [
            observation_view(
                observation,
                confirmations.get(observation.observation_id),
                reviews.get(observation.observation_id),
            )
            for observation in observations
        ],
    }


def caffeine_observations_for_day(
    session: Session,
    *,
    local_date: date,
    timezone: str,
) -> dict[str, Any]:
    start, end = local_day_bounds(local_date, timezone)
    observations = list_observations(session, start=start, end=end, limit=500)
    ids = {observation.observation_id for observation in observations}
    confirmations = latest_caffeine_confirmations(session, ids)
    reviews = latest_nutrition_reviews(session, ids)
    records: list[dict[str, Any]] = []
    for observation in observations:
        review = reviews.get(observation.observation_id)
        has_reviewed_caffeine = bool(
            review is not None
            and review.status is ConfirmationStatus.CORRECTED
            and any(
                nutrient.nutrient == "caffeine"
                and nutrient.amount.kind is not EstimateKind.UNKNOWN
                for item in review.items
                for nutrient in item.nutrients
            )
        )
        if not has_reviewed_caffeine and not any(
            item.caffeine.kind is not EstimateKind.UNKNOWN
            for item in observation.items
        ):
            continue
        records.append(
            observation_view(
                observation,
                confirmations.get(observation.observation_id),
                review,
            )
        )
    return {
        "status": "ok",
        "local_date": local_date.isoformat(),
        "timezone": timezone,
        "count": len(records),
        "observations": records,
    }


def _review_caffeine_total(
    observation: NutritionObservation,
    review: NutritionReview,
) -> float | None:
    if review.status is ConfirmationStatus.REJECTED:
        return 0.0
    if review.status is ConfirmationStatus.CORRECTED:
        estimates = [
            nutrient.amount
            for item in sorted(
                review.items, key=lambda value: value.item_index
            )
            for nutrient in item.nutrients
            if nutrient.nutrient == "caffeine"
        ]
    else:
        estimates = [item.caffeine for item in observation.items]
    if len(estimates) != len(observation.items) or any(
        estimate.kind is not EstimateKind.EXACT
        or estimate.exact is None
        for estimate in estimates
    ):
        return None
    return sum(estimate.exact for estimate in estimates if estimate.exact is not None)


def known_caffeine_for_day(
    session: Session,
    *,
    local_date: date,
    timezone: str,
) -> dict[str, Any]:
    start, end = local_day_bounds(local_date, timezone)
    observations = list_observations(session, start=start, end=end, limit=500)
    ids = {observation.observation_id for observation in observations}
    confirmations = latest_caffeine_confirmations(session, ids)
    reviews = latest_nutrition_reviews(session, ids)
    daily = latest_daily_confirmation(session, local_date, timezone)

    total_mg = 0.0
    reviewed: set[uuid.UUID] = set()
    quantified: set[uuid.UUID] = set()
    evidence: list[dict[str, Any]] = []
    for observation in observations:
        confirmation = confirmations.get(observation.observation_id)
        review = reviews.get(observation.observation_id)
        if review is not None and (
            confirmation is None
            or review.reviewed_at > confirmation.confirmed_at
        ):
            reviewed.add(observation.observation_id)
            amount = _review_caffeine_total(observation, review)
            if amount is not None:
                total_mg += amount
                quantified.add(observation.observation_id)
            evidence.append(
                {
                    "observation_id": str(observation.observation_id),
                    "confirmation_id": str(review.review_id),
                    "event_type": "nutrition.review.v1",
                    "status": review.status.value,
                    "caffeine_mg": amount,
                }
            )
            continue
        if confirmation is None:
            continue
        if confirmation.status in {
            ConfirmationStatus.CONFIRMED,
            ConfirmationStatus.CORRECTED,
        }:
            amount = sum(item.caffeine_mg for item in confirmation.items)
            total_mg += amount
            reviewed.add(observation.observation_id)
            quantified.add(observation.observation_id)
            evidence.append(
                {
                    "observation_id": str(observation.observation_id),
                    "confirmation_id": str(confirmation.confirmation_id),
                    "event_type": "nutrition.confirmation.v1",
                    "status": confirmation.status.value,
                    "caffeine_mg": amount,
                }
            )
        elif confirmation.status is ConfirmationStatus.REJECTED:
            reviewed.add(observation.observation_id)
            quantified.add(observation.observation_id)
            evidence.append(
                {
                    "observation_id": str(observation.observation_id),
                    "confirmation_id": str(confirmation.confirmation_id),
                    "event_type": "nutrition.confirmation.v1",
                    "status": confirmation.status.value,
                    "caffeine_mg": 0.0,
                }
            )

    daily_ids = set(daily.observation_ids) if daily is not None else set()
    complete = bool(
        daily is not None
        and daily.total_intake_complete
        and daily_ids == ids
        and reviewed == ids
        and quantified == ids
    )
    return {
        "status": "known" if complete else "incomplete",
        "local_date": local_date.isoformat(),
        "timezone": timezone,
        "confirmed_caffeine_mg": total_mg,
        "total_intake_complete": complete,
        "observation_count": len(observations),
        "reviewed_count": len(reviewed),
        "unreviewed_observation_ids": sorted(str(value) for value in ids - reviewed),
        "unquantified_observation_ids": sorted(
            str(value) for value in ids - quantified
        ),
        "evidence": evidence,
        "daily_confirmation_id": (
            str(daily.confirmation_id) if daily is not None else None
        ),
    }
