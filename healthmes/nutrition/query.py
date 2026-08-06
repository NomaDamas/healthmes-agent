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
    caffeine_confirmation_to_payload,
    observation_to_payload,
)
from healthmes.nutrition.repository import (
    latest_caffeine_confirmations,
    latest_daily_confirmation,
    list_observations,
    local_day_bounds,
)


def observation_view(
    observation: NutritionObservation,
    confirmation: Any | None,
) -> dict[str, Any]:
    payload = observation_to_payload(observation)
    payload["latest_confirmation"] = (
        caffeine_confirmation_to_payload(confirmation)
        if confirmation is not None
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
    return {
        "status": "ok",
        "count": len(observations),
        "observations": [
            observation_view(
                observation,
                confirmations.get(observation.observation_id),
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
    records: list[dict[str, Any]] = []
    for observation in observations:
        if not any(
            item.caffeine.kind is not EstimateKind.UNKNOWN
            for item in observation.items
        ):
            continue
        records.append(
            observation_view(
                observation,
                confirmations.get(observation.observation_id),
            )
        )
    return {
        "status": "ok",
        "local_date": local_date.isoformat(),
        "timezone": timezone,
        "count": len(records),
        "observations": records,
    }


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
    daily = latest_daily_confirmation(session, local_date, timezone)

    total_mg = 0.0
    reviewed: set[uuid.UUID] = set()
    evidence: list[dict[str, Any]] = []
    for observation in observations:
        confirmation = confirmations.get(observation.observation_id)
        if confirmation is None:
            continue
        if confirmation.status in {
            ConfirmationStatus.CONFIRMED,
            ConfirmationStatus.CORRECTED,
        }:
            amount = sum(item.caffeine_mg for item in confirmation.items)
            total_mg += amount
            reviewed.add(observation.observation_id)
            evidence.append(
                {
                    "observation_id": str(observation.observation_id),
                    "confirmation_id": str(confirmation.confirmation_id),
                    "status": confirmation.status.value,
                    "caffeine_mg": amount,
                }
            )
        elif confirmation.status is ConfirmationStatus.REJECTED:
            reviewed.add(observation.observation_id)
            evidence.append(
                {
                    "observation_id": str(observation.observation_id),
                    "confirmation_id": str(confirmation.confirmation_id),
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
        "evidence": evidence,
        "daily_confirmation_id": (
            str(daily.confirmation_id) if daily is not None else None
        ),
    }
