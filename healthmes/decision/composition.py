"""Explicit composition roots for HealthMes decision components."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars.state import SyncHealthStore
from healthmes.decision.domain_providers import (
    ActivityContextProvider,
    CalendarContextProvider,
    NutritionContextProvider,
    WearableContextProvider,
    WearableReader,
)
from healthmes.decision.providers import ContextProviderRegistry
from healthmes.store.enums import CalendarSource


def build_context_provider_registry(
    *,
    wearable_reader: WearableReader | None = None,
    session_factory: sessionmaker[Session] | None = None,
    calendar_sync_health_store: SyncHealthStore | None = None,
    calendar_sources: tuple[CalendarSource, ...] = (),
) -> ContextProviderRegistry:
    """Build the default registry without import-time global registration."""

    return ContextProviderRegistry(
        (
            ActivityContextProvider(),
            NutritionContextProvider(),
            WearableContextProvider(
                wearable_reader,
                snapshot_session_factory=session_factory,
            ),
            CalendarContextProvider(
                sync_health_store=calendar_sync_health_store,
                sources=calendar_sources,
            ),
        )
    )
