"""Lineage contracts for Open Wearables capability execution."""

from __future__ import annotations

from enum import StrEnum


class OpenWearablesLineageMode(StrEnum):
    """How a capability proves that returned rows belong to the user source."""

    EXPLICIT_ROW_SOURCE_ID = "explicit_row_source_id"
    PROVIDER_ROUTE_AUTHORITATIVE = "provider_route_authoritative"
    LINEAGE_UNAVAILABLE = "lineage_unavailable"


OPEN_WEARABLES_EXPLICIT_LINEAGE_CAPABILITIES = frozenset(
    {
        "wearable.health-scores",
        "wearable.whoop-recovery-package",
    }
)
OPEN_WEARABLES_PROVIDER_ROUTE_LINEAGE_CAPABILITIES = frozenset(
    {
        "wearable.provider-workouts",
        "wearable.provider-workout-detail",
    }
)


def open_wearables_lineage_mode(
    capability: str,
) -> OpenWearablesLineageMode:
    """Return the strict source-lineage contract for one capability.

    ``LINEAGE_UNAVAILABLE`` means the upstream route does not guarantee
    source attribution for every row. The capability may still be cataloged,
    but the bounded adapter accepts only rows carrying an exact frozen source
    ID and rejects all unattributed rows.
    """

    normalized = capability.strip().casefold()
    if normalized in OPEN_WEARABLES_EXPLICIT_LINEAGE_CAPABILITIES:
        return OpenWearablesLineageMode.EXPLICIT_ROW_SOURCE_ID
    if normalized in OPEN_WEARABLES_PROVIDER_ROUTE_LINEAGE_CAPABILITIES:
        return OpenWearablesLineageMode.PROVIDER_ROUTE_AUTHORITATIVE
    return OpenWearablesLineageMode.LINEAGE_UNAVAILABLE


__all__ = [
    "OPEN_WEARABLES_EXPLICIT_LINEAGE_CAPABILITIES",
    "OPEN_WEARABLES_PROVIDER_ROUTE_LINEAGE_CAPABILITIES",
    "OpenWearablesLineageMode",
    "open_wearables_lineage_mode",
]
