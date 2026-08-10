package com.healthmes.usagecollector.net

import org.junit.Assert.assertEquals
import org.junit.Test

class IngestFailurePolicyTest {

    @Test
    fun `configuration and concurrent writer conflicts are retried`() {
        listOf(
            "stale_collection_revision",
            "stale_collection_generation",
            "activity_collection_generation_unregistered",
            "activity_collection_blocked",
            "activity_write_conflict",
        ).forEach { code ->
            assertEquals(
                ActivityConflictDisposition.RETRY,
                activityConflictDisposition(code),
            )
        }
    }

    @Test
    fun `permanent activity conflicts isolate only rejected samples`() {
        listOf(
            "activity_outside_retention",
            "activity_future_data",
            "activity_source_mode_conflict",
            "activity_source_conflict",
        ).forEach { code ->
            assertEquals(
                ActivityConflictDisposition.ISOLATE_REJECTED_SAMPLE,
                activityConflictDisposition(code),
            )
        }
    }

    @Test
    fun `summary scope and unknown conflicts fail closed`() {
        listOf(
            "activity_summary_requires_complete_raw",
            null,
            "",
            "unknown_conflict",
        ).forEach { code ->
            assertEquals(
                ActivityConflictDisposition.FAIL_CLOSED,
                activityConflictDisposition(code),
            )
        }
    }
}
