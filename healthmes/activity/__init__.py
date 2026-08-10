"""Activity telemetry intake, aggregation, and decision context.

The package owns phone/computer activity only. Wearable, calendar, and
nutrition data keep their existing ingestion paths and meet activity data at
the bounded cross-domain context layer.
"""

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityCollectionUpdate,
    ActivityPlatform,
    ActivityRecord,
    ActivityState,
    AppHourRecord,
    AppIntervalRecord,
)

__all__ = [
    "ActivityBatchIn",
    "ActivityCapability",
    "ActivityCollectionUpdate",
    "ActivityPlatform",
    "ActivityRecord",
    "ActivityState",
    "AppHourRecord",
    "AppIntervalRecord",
]
