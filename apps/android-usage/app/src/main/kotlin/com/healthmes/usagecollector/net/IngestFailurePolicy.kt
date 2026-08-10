package com.healthmes.usagecollector.net

internal enum class ActivityConflictDisposition {
    RETRY,
    FAIL_CLOSED,
}

/**
 * Only conflicts that can change after a fresh config read or a concurrent
 * writer finishes are retried. Every deterministic data conflict fails the
 * complete authoritative snapshot without advancing the watermark.
 */
internal fun activityConflictDisposition(
    errorCode: String?,
): ActivityConflictDisposition =
    when (errorCode) {
        "stale_collection_revision",
        "stale_collection_generation",
        "activity_collection_generation_unregistered",
        "activity_collection_blocked",
        "activity_write_conflict",
        -> ActivityConflictDisposition.RETRY

        "activity_outside_retention",
        "activity_future_data",
        "activity_source_conflict",
        "activity_source_mode_conflict",
        "activity_summary_requires_complete_raw" ->
            ActivityConflictDisposition.FAIL_CLOSED

        else -> ActivityConflictDisposition.FAIL_CLOSED
    }
