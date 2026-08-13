package com.healthmes.usagecollector.work

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

class CollectionWindowPolicyTest {

    @Test
    fun `permission revocation moves the readable boundary to now`() {
        val revokedAtMs = 1_785_556_321_000L

        val boundary = revokedPermissionBoundary(revokedAtMs)
        val queryWindow = activityQueryWindow(
            collectionSinceMs = boundary.collectionSinceMs,
            watermarkMs = boundary.watermarkMs,
            nowMs = revokedAtMs + 30 * 60 * 1000,
        )

        assertEquals(revokedAtMs, boundary.collectionSinceMs)
        assertTrue(boundary.watermarkMs <= revokedAtMs)
        assertTrue(queryWindow.beginMs >= revokedAtMs)
    }

    @Test
    fun `offline backlog is paged instead of truncated`() {
        val nowMs = 30L * 24 * 60 * 60 * 1000
        val window = activityQueryWindow(
            collectionSinceMs = 0,
            watermarkMs = 24L * 60 * 60 * 1000,
            nowMs = nowMs,
        )

        assertEquals(18L * 60 * 60 * 1000, window.beginMs)
        assertEquals(
            window.beginMs + 7L * 24 * 60 * 60 * 1000,
            window.endMs,
        )
        assertTrue(window.hasMoreBacklog)
    }

    @Test
    fun `unaligned backlog page keeps its final partial hour provisional`() {
        val hourMs = 60L * 60L * 1000L
        val collectionSinceMs = hourMs + 17L * 60L * 1000L
        val window = activityQueryWindow(
            collectionSinceMs = collectionSinceMs,
            watermarkMs = collectionSinceMs,
            nowMs = collectionSinceMs + 10L * 24L * hourMs,
        )
        val finalBucketStartMs = window.endMs - window.endMs % hourMs

        assertTrue(window.hasMoreBacklog)
        assertFalse(
            bucketIsComplete(
                bucketStartMs = finalBucketStartMs,
                queryBeginMs = window.beginMs,
                queryEndMs = window.endMs,
                collectionSinceMs = collectionSinceMs,
                nowMs = collectionSinceMs + 10L * 24L * hourMs,
            )
        )
        assertTrue(
            bucketIsComplete(
                bucketStartMs = finalBucketStartMs - hourMs,
                queryBeginMs = window.beginMs,
                queryEndMs = window.endMs,
                collectionSinceMs = collectionSinceMs,
                nowMs = collectionSinceMs + 10L * 24L * hourMs,
            )
        )
    }

    @Test
    fun `recently closed hour remains provisional during settlement grace`() {
        val hourMs = 60L * 60L * 1000L
        val bucketStartMs = 10L * hourMs
        val bucketEndMs = bucketStartMs + hourMs

        assertFalse(
            bucketIsComplete(
                bucketStartMs = bucketStartMs,
                queryBeginMs = bucketStartMs,
                queryEndMs = bucketEndMs,
                collectionSinceMs = bucketStartMs,
                nowMs = bucketEndMs + BUCKET_SETTLEMENT_GRACE_MS - 1L,
            )
        )
        assertTrue(
            bucketIsComplete(
                bucketStartMs = bucketStartMs,
                queryBeginMs = bucketStartMs,
                queryEndMs = bucketEndMs,
                collectionSinceMs = bucketStartMs,
                nowMs = bucketEndMs + BUCKET_SETTLEMENT_GRACE_MS,
            )
        )
    }

    @Test
    fun `persisted snapshot sequence advances in same millisecond and rollback`() {
        var persistedSequence = 1_000L
        val first = reservePersistedSnapshotSequence(
            previousSequence = persistedSequence,
            wallClockMs = 1_000L,
            persist = {
                persistedSequence = it
                true
            },
        )
        val second = reservePersistedSnapshotSequence(
            previousSequence = checkNotNull(first),
            wallClockMs = 900L,
            persist = {
                persistedSequence = it
                true
            },
        )

        assertEquals(1_001L, first)
        assertEquals(1_002L, second)
        assertEquals(1_002L, persistedSequence)
        assertNull(
            reservePersistedSnapshotSequence(
                previousSequence = Long.MAX_VALUE,
                wallClockMs = 1_100L,
                persist = { error("overflow must not be persisted") },
            )
        )
    }

    @Test
    fun `wall clock regression requests a boundary and never reverses query range`() {
        val window = activityQueryWindow(
            collectionSinceMs = 20_000L,
            watermarkMs = 18_000L,
            nowMs = 10_000L,
        )

        assertTrue(
            clockRegressionBoundaryRequired(
                collectionSinceMs = 20_000L,
                watermarkMs = 18_000L,
                nowMs = 10_000L,
            )
        )
        assertEquals(10_000L, window.beginMs)
        assertEquals(10_000L, window.endMs)
        assertFalse(window.hasReadableRange)
        assertTrue(window.beginMs <= window.endMs)
    }

    @Test
    fun `stopped work cannot continue a sensitive operation`() {
        assertFalse(
            sensitiveContinuationAllowed(
                isStopped = true,
                permissionGranted = true,
                guardCurrent = true,
            )
        )
        assertTrue(
            sensitiveContinuationAllowed(
                isStopped = false,
                permissionGranted = true,
                guardCurrent = true,
            )
        )
    }

    @Test
    fun `manual upload cannot bypass collection off or durable quarantine`() {
        assertFalse(
            uploadAllowed(
                collectionEnabled = false,
                collectionQuarantined = false,
            )
        )
        assertFalse(
            uploadAllowed(
                collectionEnabled = true,
                collectionQuarantined = true,
            ),
        )
    }

    @Test
    fun `persisted boundary generation invalidates an in flight upload`() {
        assertEquals(
            false,
            collectionWindowLeaseAllowed(
                expectedGeneration = 7,
                currentGeneration = 8,
                collectionEnabled = true,
                collectionQuarantined = false,
            ),
        )
        assertTrue(
            collectionWindowLeaseAllowed(
                expectedGeneration = 8,
                currentGeneration = 8,
                collectionEnabled = true,
                collectionQuarantined = false,
            )
        )
    }

    @Test
    fun `timezone change requires a new collection boundary`() {
        assertTrue(
            timezoneBoundaryRequired(
                storedTimezone = "America/Los_Angeles",
                currentTimezone = "Asia/Seoul",
            )
        )
        assertTrue(
            timezoneBoundaryRequired(
                storedTimezone = null,
                currentTimezone = "UTC",
            )
        )
        assertFalse(
            timezoneBoundaryRequired(
                storedTimezone = "UTC",
                currentTimezone = "UTC",
            )
        )
    }

    @Test
    fun `privacy state is committed only behind an armed durable quarantine`() {
        val calls = mutableListOf<String>()

        assertFalse(
            commitFailClosedBoundary(
                armQuarantine = {
                    calls += "arm"
                    false
                },
                commitState = {
                    calls += "commit"
                    true
                },
                clearQuarantine = {
                    calls += "clear"
                    true
                },
            )
        )
        assertEquals(listOf("arm"), calls)

        calls.clear()
        assertFalse(
            commitFailClosedBoundary(
                armQuarantine = {
                    calls += "arm"
                    true
                },
                commitState = {
                    calls += "commit"
                    false
                },
                clearQuarantine = {
                    calls += "clear"
                    true
                },
            )
        )
        assertEquals(listOf("arm", "commit"), calls)

        calls.clear()
        assertTrue(
            commitFailClosedBoundary(
                armQuarantine = {
                    calls += "arm"
                    true
                },
                commitState = {
                    calls += "commit"
                    true
                },
                clearQuarantine = {
                    calls += "clear"
                    true
                },
            )
        )
        assertEquals(listOf("arm", "commit", "clear"), calls)
    }

    @Test
    fun `cancelled waiter exits before the upload gate owner releases`() {
        val ownerEntered = CountDownLatch(1)
        val releaseOwner = CountDownLatch(1)
        val waiterStarted = CountDownLatch(1)
        val waiterFinished = CountDownLatch(1)
        val cancelled = AtomicBoolean(false)
        val waiterBodyInvoked = AtomicBoolean(false)
        val waiterResult = AtomicReference<String?>()

        val owner = Thread {
            UploadExecutionGate.runCancellable(
                isCancelled = { false },
                onCancelled = { error("owner was unexpectedly cancelled") },
            ) {
                ownerEntered.countDown()
                releaseOwner.await(5, TimeUnit.SECONDS)
            }
        }
        val waiter = Thread {
            waiterStarted.countDown()
            waiterResult.set(
                UploadExecutionGate.runCancellable(
                    isCancelled = { cancelled.get() },
                    onCancelled = { "cancelled" },
                ) {
                    waiterBodyInvoked.set(true)
                    "ran"
                }
            )
            waiterFinished.countDown()
        }

        owner.start()
        assertTrue(ownerEntered.await(2, TimeUnit.SECONDS))
        waiter.start()
        assertTrue(waiterStarted.await(2, TimeUnit.SECONDS))
        Thread.sleep(100)
        assertEquals(1L, waiterFinished.count)

        cancelled.set(true)
        try {
            assertTrue(waiterFinished.await(2, TimeUnit.SECONDS))
            assertEquals("cancelled", waiterResult.get())
            assertFalse(waiterBodyInvoked.get())
        } finally {
            releaseOwner.countDown()
            owner.join(2_000)
            waiter.join(2_000)
        }

        assertFalse(owner.isAlive)
        assertFalse(waiter.isAlive)
    }

    @Test
    fun `cancellation is checked again immediately after gate acquisition`() {
        val cancellationChecks = AtomicInteger()
        val bodyInvoked = AtomicBoolean(false)

        val result = UploadExecutionGate.runCancellable(
            isCancelled = { cancellationChecks.incrementAndGet() >= 2 },
            onCancelled = { "cancelled" },
        ) {
            bodyInvoked.set(true)
            "ran"
        }

        assertEquals("cancelled", result)
        assertEquals(2, cancellationChecks.get())
        assertFalse(bodyInvoked.get())
    }

    @Test
    fun `snapshot buckets include empty and partial hours without splitting them`() {
        val hourMs = 60L * 60L * 1000L
        assertEquals(
            listOf(0L, hourMs, 2 * hourMs),
            bucketStartsForWindow(
                beginMs = 15 * 60_000L,
                endMs = 2 * hourMs + 1L,
            ),
        )
    }

    @Test
    fun `empty query window produces no snapshot buckets`() {
        val hourMs = 60L * 60L * 1000L
        assertEquals(
            emptyList<Long>(),
            bucketStartsForWindow(
                beginMs = hourMs,
                endMs = hourMs,
            ),
        )
    }
}
