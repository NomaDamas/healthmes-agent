package com.healthmes.usagecollector.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SnapshotChunkingTest {

    @Test
    fun `hourly snapshot manifests are never split across HTTP chunks`() {
        fun snapshot(hour: Int, apps: Int): UploadBucketSnapshot {
            val start = "2026-08-01T%02d:00:00Z".format(hour)
            return UploadBucketSnapshot(
                bucketStartIso = start,
                bucketComplete = true,
                snapshotSequence = 10L,
                samples = (0 until apps).map { index ->
                    UploadSample(
                        bucketStartIso = start,
                        appPackage = "app-$hour-$index",
                        foregroundSeconds = 1,
                        launches = 1,
                        category = null,
                        snapshotSequence = 10L,
                    )
                },
            )
        }

        val first = snapshot(hour = 1, apps = 3)
        val second = snapshot(hour = 2, apps = 3)
        val empty = snapshot(hour = 3, apps = 0).copy(
            sourceSetComplete = false,
        )

        val chunks = packSnapshotChunks(
            listOf(first, second, empty),
            maxSamples = 5,
            maxSnapshots = 10,
        )

        assertEquals(listOf(listOf(first), listOf(second, empty)), chunks)
        assertFalse(empty.sourceSetComplete)
    }

    @Test
    fun `accepted response is not mislabeled as cancelled after boundary change`() {
        val outcome = postResponseBoundaryOutcome(
            IngestClient.Outcome.Success(samplesSent = 3),
            boundaryStillCurrent = false,
        )

        assertTrue(outcome is IngestClient.Outcome.Success)
        outcome as IngestClient.Outcome.Success
        assertEquals(3, outcome.samplesSent)
        assertTrue(outcome.boundaryChangedAfterCommit)
    }

    @Test
    fun `uncommitted failure becomes cancelled after boundary change`() {
        val outcome = postResponseBoundaryOutcome(
            IngestClient.Outcome.TransientFailure("network"),
            boundaryStillCurrent = false,
        )

        assertFalse(outcome is IngestClient.Outcome.Success)
        assertEquals(IngestClient.Outcome.Cancelled, outcome)
    }
}
