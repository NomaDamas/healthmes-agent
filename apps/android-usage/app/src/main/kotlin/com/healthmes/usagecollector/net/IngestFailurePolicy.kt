package com.healthmes.usagecollector.net

internal enum class ActivityConflictDisposition {
    RETRY,
    ISOLATE_REJECTED_SAMPLE,
    FAIL_CLOSED,
}

/**
 * Only conflicts that can change after a fresh config read or a concurrent
 * writer finishes are retried. Explicit sample-local conflicts are bisected;
 * unknown or malformed 409s stop without advancing the watermark.
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
        -> ActivityConflictDisposition.ISOLATE_REJECTED_SAMPLE

        // These conflicts cover a provider/device or summary scope, not one
        // bad sample. Bisecting would discard every sample and then advance
        // the watermark across data that was never accepted.
        "activity_source_mode_conflict",
        "activity_summary_requires_complete_raw" ->
            ActivityConflictDisposition.FAIL_CLOSED

        else -> ActivityConflictDisposition.FAIL_CLOSED
    }
