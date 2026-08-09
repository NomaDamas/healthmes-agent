package com.healthmes.usagecollector.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PartialUploadIsolationTest {

    private fun sample(hour: Int, app: String): UploadSample =
        UploadSample(
            bucketStartIso = "2026-08-01T%02d:00:00Z".format(hour),
            appPackage = app,
            foregroundSeconds = 60,
            launches = 1,
            category = "productivity",
        )

    @Test
    fun `one permanent bad sample is isolated without dropping later samples`() {
        val samples = listOf(
            sample(10, "good.first"),
            sample(11, "bad"),
            sample(12, "good.last"),
        )
        val accepted = mutableListOf<String>()

        val result = uploadWithIsolation(samples, maxChunkSize = 2) { chunk ->
            if (chunk.any { it.appPackage == "bad" }) {
                ChunkUploadResult.IsolatableFailure("activity_future_data")
            } else {
                accepted += chunk.map(UploadSample::appPackage)
                ChunkUploadResult.Success
            }
        }

        assertEquals(listOf("good.first", "good.last"), accepted)
        assertEquals(2, result.sent)
        assertEquals(listOf(samples[1]), result.discarded)
        assertTrue(result.failure == null)
        assertEquals(false, result.cancelled)
    }

    @Test
    fun `transient failure keeps the full watermark range retryable`() {
        val samples = listOf(
            sample(10, "good.first"),
            sample(11, "retry"),
            sample(12, "not-attempted"),
        )

        val result = uploadWithIsolation(samples, maxChunkSize = 1) { chunk ->
            if (chunk.single().appPackage == "retry") {
                ChunkUploadResult.TransientFailure("server unavailable")
            } else {
                ChunkUploadResult.Success
            }
        }

        assertEquals(1, result.sent)
        assertTrue(result.discarded.isEmpty())
        assertEquals("server unavailable", result.failure?.reason)
        assertEquals(false, result.cancelled)
    }

    @Test
    fun `batch level permanent failure stops without discarding samples`() {
        val samples = listOf(
            sample(10, "unknown"),
            sample(11, "not-attempted"),
        )
        var attempts = 0

        val result = uploadWithIsolation(samples, maxChunkSize = 1) {
            attempts += 1
            ChunkUploadResult.PermanentFailure("unknown conflict")
        }

        assertEquals(1, attempts)
        assertEquals(0, result.sent)
        assertTrue(result.discarded.isEmpty())
        assertEquals("unknown conflict", result.failure?.reason)
        assertEquals(false, result.failure?.transient)
        assertEquals(false, result.cancelled)
    }

    @Test
    fun `privacy boundary cancels before the next chunk without advancing the pass`() {
        val samples = listOf(
            sample(10, "sent-before-boundary"),
            sample(11, "must-not-send"),
        )
        var allow = true
        val attempted = mutableListOf<String>()

        val result = uploadWithIsolation(
            samples,
            maxChunkSize = 1,
            shouldContinue = { allow },
        ) { chunk ->
            attempted += chunk.single().appPackage
            allow = false
            ChunkUploadResult.Success
        }

        assertEquals(listOf("sent-before-boundary"), attempted)
        assertEquals(1, result.sent)
        assertTrue(result.discarded.isEmpty())
        assertTrue(result.cancelled)
        assertTrue(result.failure == null)
    }
}
