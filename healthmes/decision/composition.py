"""Explicit composition roots for HealthMes decision components."""

from __future__ import annotations

from healthmes.decision.domain_providers import (
    ActivityContextProvider,
    CalendarContextProvider,
    NutritionContextProvider,
    WearableContextProvider,
    WearableReader,
)
from healthmes.decision.providers import ContextProviderRegistry


def build_context_provider_registry(
    *,
    wearable_reader: WearableReader | None = None,
) -> ContextProviderRegistry:
    """Build the default registry without import-time global registration."""

    return ContextProviderRegistry(
        (
            ActivityContextProvider(),
            NutritionContextProvider(),
            WearableContextProvider(wearable_reader),
            CalendarContextProvider(),
        )
    )

