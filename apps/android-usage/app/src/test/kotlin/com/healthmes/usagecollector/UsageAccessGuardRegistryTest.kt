package com.healthmes.usagecollector

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

class UsageAccessGuardRegistryTest {

    @Before
    fun resetBefore() {
        UsageAccessGuardRegistry.invalidate()
    }

    @After
    fun resetAfter() {
        UsageAccessGuardRegistry.invalidate()
    }

    @Test
    fun `token is process local and invalidated only by its owner`() {
        val token = publish("process-a", 7)

        UsageAccessGuardRegistry.invalidate("process-b")

        assertEquals(token, UsageAccessGuardRegistry.snapshot())
        assertTrue(UsageAccessGuardRegistry.isCurrent(token))

        UsageAccessGuardRegistry.invalidate("process-a")

        assertNull(UsageAccessGuardRegistry.snapshot())
        assertFalse(UsageAccessGuardRegistry.isCurrent(token))
    }

    @Test
    fun `generation advances only from the current token`() {
        val current = publish("process-a", 7)
        val stale = current.copy(collectionGeneration = 6)

        assertNull(
            UsageAccessGuardRegistry.advanceGeneration(
                expected = stale,
                collectionGeneration = 8,
            )
        )
        val advanced = UsageAccessGuardRegistry.advanceGeneration(
            expected = current,
            collectionGeneration = 8,
        )

        assertEquals(
            current.copy(collectionGeneration = 8),
            advanced,
        )
        assertEquals(advanced, UsageAccessGuardRegistry.snapshot())
    }

    @Test
    fun `closed service cannot publish a callback result`() {
        val lease = UsageAccessGuardRegistry.activateService("process-a")
        val publication = checkNotNull(
            UsageAccessGuardRegistry.beginPublication(lease)
        )

        UsageAccessGuardRegistry.closeService(lease)

        assertNull(
            UsageAccessGuardRegistry.publish(
                publication = publication,
                collectionGeneration = 8,
                pairingRevision = 3L,
            )
        )
        assertNull(UsageAccessGuardRegistry.snapshot())
    }

    @Test
    fun `settings fence makes an in-flight publication stale and rechecks once`() {
        val rechecks = AtomicInteger()
        val lease = UsageAccessGuardRegistry.activateService("process-a") {
            rechecks.incrementAndGet()
        }
        val publication = checkNotNull(
            UsageAccessGuardRegistry.beginPublication(lease)
        )

        val committed = UsageAccessGuardRegistry.withBoundaryFence { true }

        assertTrue(committed)
        assertNull(
            UsageAccessGuardRegistry.publish(
                publication = publication,
                collectionGeneration = 8,
                pairingRevision = 3L,
            )
        )
        assertEquals(1, rechecks.get())
    }

    @Test
    fun `boundary invalidation completes during a blocked read and discards it`() {
        val token = publish("process-a", 7)
        val readStarted = CountDownLatch(1)
        val releaseRead = CountDownLatch(1)
        val invalidated = CountDownLatch(1)
        val readResult = AtomicReference<String?>("not-completed")

        val reader = Thread {
            readResult.set(
                UsageAccessGuardRegistry.readIfCurrent(token) {
                    readStarted.countDown()
                    releaseRead.await(5, TimeUnit.SECONDS)
                    "sensitive"
                }
            )
        }
        val invalidator = Thread {
            readStarted.await(2, TimeUnit.SECONDS)
            UsageAccessGuardRegistry.withBoundaryFence { Unit }
            invalidated.countDown()
        }
        reader.start()
        invalidator.start()

        assertTrue(readStarted.await(2, TimeUnit.SECONDS))
        assertTrue(invalidated.await(2, TimeUnit.SECONDS))
        assertNull(UsageAccessGuardRegistry.snapshot())
        releaseRead.countDown()
        reader.join(2_000)
        invalidator.join(2_000)

        assertFalse(reader.isAlive)
        assertFalse(invalidator.isAlive)
        assertNull(readResult.get())
    }

    @Test
    fun `stale token never acquires a read lease`() {
        val current = publish("process-a", 7)
        val invoked = AtomicBoolean(false)

        val value = UsageAccessGuardRegistry.readIfCurrent(
            current.copy(collectionGeneration = 6),
        ) {
            invoked.set(true)
            "sensitive"
        }

        assertNull(value)
        assertFalse(invoked.get())
    }

    @Test
    fun `publication blocked by a fence requests one deferred recheck`() {
        val rechecks = AtomicInteger()
        val lease = UsageAccessGuardRegistry.activateService("process-a") {
            rechecks.incrementAndGet()
        }

        UsageAccessGuardRegistry.withBoundaryFence {
            assertNull(UsageAccessGuardRegistry.beginPublication(lease))
            assertEquals(0, rechecks.get())
        }

        assertEquals(1, rechecks.get())
    }

    @Test
    fun `nested fences request only one deferred recheck`() {
        val rechecks = AtomicInteger()
        UsageAccessGuardRegistry.activateService("process-a") {
            rechecks.incrementAndGet()
        }

        UsageAccessGuardRegistry.withBoundaryFence {
            UsageAccessGuardRegistry.withBoundaryFence {
                assertEquals(0, rechecks.get())
            }
            assertEquals(0, rechecks.get())
        }

        assertEquals(1, rechecks.get())
    }

    @Test
    fun `closing the service inside a fence cancels deferred recheck`() {
        val rechecks = AtomicInteger()
        val lease = UsageAccessGuardRegistry.activateService("process-a") {
            rechecks.incrementAndGet()
        }

        UsageAccessGuardRegistry.withBoundaryFence {
            UsageAccessGuardRegistry.closeService(lease)
        }

        assertEquals(0, rechecks.get())
    }

    @Test
    fun `owner invalidation stales a publication before a token exists`() {
        val lease = UsageAccessGuardRegistry.activateService("process-a")
        val publication = checkNotNull(
            UsageAccessGuardRegistry.beginPublication(lease)
        )

        UsageAccessGuardRegistry.invalidate("process-a")

        assertNull(
            UsageAccessGuardRegistry.publish(
                publication = publication,
                collectionGeneration = 8,
                pairingRevision = 3L,
            )
        )
    }

    @Test
    fun `observed revoke fences token before delayed reevaluation`() {
        val lease = UsageAccessGuardRegistry.activateService("process-a")
        val publication = checkNotNull(
            UsageAccessGuardRegistry.beginPublication(lease)
        )
        val token = checkNotNull(
            UsageAccessGuardRegistry.publish(
                publication = publication,
                collectionGeneration = 7,
                pairingRevision = 3L,
            )
        )
        val callbackFenced = CountDownLatch(1)
        val allowReevaluation = CountDownLatch(1)

        val callback = Thread {
            assertTrue(
                UsageAccessGuardRegistry.invalidateForObservedBoundary(lease)
            )
            callbackFenced.countDown()
            allowReevaluation.await(5, TimeUnit.SECONDS)
        }
        callback.start()

        assertTrue(callbackFenced.await(2, TimeUnit.SECONDS))
        assertNull(UsageAccessGuardRegistry.snapshot())
        assertFalse(UsageAccessGuardRegistry.isCurrent(token))
        assertNull(
            UsageAccessGuardRegistry.publish(
                publication = publication,
                collectionGeneration = 7,
                pairingRevision = 3L,
            )
        )

        allowReevaluation.countDown()
        callback.join(2_000)
        assertFalse(callback.isAlive)
    }

    @Test
    fun `cancelled continuation never enters sensitive read`() {
        val token = publish("process-a", 7)
        val checks = AtomicInteger()
        val readInvoked = AtomicBoolean(false)

        val result = UsageAccessGuardRegistry.readIfCurrent(
            expected = token,
            shouldContinue = {
                checks.incrementAndGet() < 2
            },
        ) {
            readInvoked.set(true)
            "sensitive"
        }

        assertNull(result)
        assertEquals(2, checks.get())
        assertFalse(readInvoked.get())
    }

    private fun publish(
        processEpoch: String,
        collectionGeneration: Long,
    ): UsageAccessGuardToken {
        val lease = UsageAccessGuardRegistry.activateService(processEpoch)
        val publication = checkNotNull(
            UsageAccessGuardRegistry.beginPublication(lease)
        )
        return checkNotNull(
            UsageAccessGuardRegistry.publish(
                publication = publication,
                collectionGeneration = collectionGeneration,
                pairingRevision = 3L,
            )
        )
    }
}
