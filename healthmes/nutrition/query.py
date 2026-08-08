"""Read-only nutrition evidence views for MCP and decision skills."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
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
from healthmes.nutrition.intake_contracts import (
    EvidenceOrigin,
    IntakeOutcome,
    IntakeOutcomeStatus,
    NormalizedIntakeItem,
)
from healthmes.nutrition.repository import (
    intake_outcome_states_for_day,
    latest_caffeine_confirmations,
    latest_daily_confirmation,
    latest_intake_outcome_states,
    latest_nutrition_reviews,
    list_observations,
    local_day_bounds,
)

INTAKE_OUTCOME_EVENT = "nutrition.intake-outcome.v1"


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


def _item_caffeine_total(
    items: tuple[NormalizedIntakeItem, ...],
) -> float | None:
    total = 0.0
    for item in items:
        estimates = [
            fact.amount
            for fact in item.nutrients
            if fact.nutrient.casefold() == "caffeine"
        ]
        if (
            len(estimates) != 1
            or estimates[0].kind is not EstimateKind.EXACT
            or estimates[0].unit.casefold() != "mg"
            or estimates[0].exact is None
            or next(
                fact.origin
                for fact in item.nutrients
                if fact.nutrient.casefold() == "caffeine"
            )
            not in {EvidenceOrigin.USER, EvidenceOrigin.LABEL}
        ):
            return None
        total += estimates[0].exact
    return total


def _outcome_is_consumed_in_day(
    outcome: IntakeOutcome,
    *,
    start: datetime,
    end: datetime,
) -> bool:
    return bool(
        outcome.status is IntakeOutcomeStatus.CONSUMED
        and outcome.consumed_at is not None
        and start <= outcome.consumed_at.astimezone(UTC) < end
    )


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
    outcome_states = intake_outcome_states_for_day(
        session,
        start=start,
        end=end,
    )
    latest_outcomes = latest_intake_outcome_states(session)

    total_mg = 0.0
    reviewed: set[uuid.UUID] = set()
    quantified: set[uuid.UUID] = set()
    quantified_outcomes: set[uuid.UUID] = set()
    outcome_observation_ids = {
        snapshot.nutrition_observation_id
        for _, outcome in latest_outcomes.values()
        if (snapshot := outcome.intake_snapshot) is not None
        and snapshot.nutrition_observation_id is not None
    }
    evidence: list[dict[str, Any]] = []
    latest_evidence_at: datetime | None = None
    consumed_outcome_count = 0
    for row, outcome in outcome_states:
        snapshot = outcome.intake_snapshot
        included_in_total = _outcome_is_consumed_in_day(
            outcome,
            start=start,
            end=end,
        )
        amount = (
            _item_caffeine_total(snapshot.items)
            if snapshot is not None and included_in_total
            else None
        )
        if included_in_total:
            consumed_outcome_count += 1
        if included_in_total and amount is not None:
            total_mg += amount
            quantified_outcomes.add(outcome.outcome_id)
        latest_evidence_at = max(
            latest_evidence_at or outcome.confirmed_at,
            outcome.confirmed_at,
        )
        evidence.append(
            {
                "event_id": str(row.id),
                "interaction_id": str(outcome.interaction_id),
                "outcome_id": str(outcome.outcome_id),
                "event_type": INTAKE_OUTCOME_EVENT,
                "status": outcome.status.value,
                "caffeine_mg": amount if included_in_total else 0.0,
                "included_in_total": included_in_total,
                "modality": (
                    snapshot.modality.value if snapshot is not None else None
                ),
                "nutrition_observation_id": (
                    str(snapshot.nutrition_observation_id)
                    if snapshot is not None
                    and snapshot.nutrition_observation_id is not None
                    else None
                ),
                "nutrition_review_id": (
                    str(snapshot.nutrition_review_id)
                    if snapshot is not None
                    and snapshot.nutrition_review_id is not None
                    else None
                ),
            }
        )
    for observation in observations:
        if observation.observation_id in outcome_observation_ids:
            reviewed.add(observation.observation_id)
            quantified.add(observation.observation_id)
            continue
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
            latest_evidence_at = max(
                latest_evidence_at or review.reviewed_at,
                review.reviewed_at,
            )
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
            latest_evidence_at = max(
                latest_evidence_at or confirmation.confirmed_at,
                confirmation.confirmed_at,
            )
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
            latest_evidence_at = max(
                latest_evidence_at or confirmation.confirmed_at,
                confirmation.confirmed_at,
            )
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
    daily_outcome_ids = set(daily.outcome_ids) if daily is not None else set()
    outcome_state_ids = {
        outcome.outcome_id for _, outcome in outcome_states
    }
    included_outcome_ids = {
        outcome.outcome_id
        for _, outcome in outcome_states
        if _outcome_is_consumed_in_day(outcome, start=start, end=end)
    }
    complete = bool(
        daily is not None
        and daily.total_intake_complete
        and daily_ids == ids
        and reviewed == ids
        and quantified == ids
        and daily_outcome_ids == outcome_state_ids
        and quantified_outcomes
        == included_outcome_ids
        and (
            latest_evidence_at is None
            or daily.confirmed_at.astimezone(UTC)
            >= latest_evidence_at.astimezone(UTC)
        )
    )
    return {
        "status": "known" if complete else "incomplete",
        "local_date": local_date.isoformat(),
        "timezone": timezone,
        "confirmed_caffeine_mg": total_mg,
        "total_intake_complete": complete,
        "observation_count": len(observations),
        "consumed_outcome_count": consumed_outcome_count,
        "outcome_state_count": len(outcome_states),
        "ledger_entry_count": len(observations) + consumed_outcome_count,
        "reviewed_count": len(reviewed),
        "unreviewed_observation_ids": sorted(str(value) for value in ids - reviewed),
        "unquantified_observation_ids": sorted(
            str(value) for value in ids - quantified
        ),
        "unquantified_outcome_ids": sorted(
            str(outcome.outcome_id)
            for _, outcome in outcome_states
            if _outcome_is_consumed_in_day(outcome, start=start, end=end)
            and outcome.outcome_id not in quantified_outcomes
        ),
        "evidence": evidence,
        "daily_confirmation_id": (
            str(daily.confirmation_id) if daily is not None else None
        ),
    }
