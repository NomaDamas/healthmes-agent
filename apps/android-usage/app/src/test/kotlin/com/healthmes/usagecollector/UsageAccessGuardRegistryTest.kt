package com.healthmes.usagecollector

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

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
        val token = UsageAccessGuardToken(
            processEpoch = "process-a",
            collectionGeneration = 7,
        )
        UsageAccessGuardRegistry.publish(token)

        UsageAccessGuardRegistry.invalidate("process-b")

        assertEquals(token, UsageAccessGuardRegistry.snapshot())
        assertTrue(UsageAccessGuardRegistry.isCurrent(token))

        UsageAccessGuardRegistry.invalidate("process-a")

        assertNull(UsageAccessGuardRegistry.snapshot())
        assertFalse(UsageAccessGuardRegistry.isCurrent(token))
    }

    @Test
    fun `generation advances only from the current token`() {
        val current = UsageAccessGuardToken(
            processEpoch = "process-a",
            collectionGeneration = 7,
        )
        val stale = UsageAccessGuardToken(
            processEpoch = "process-a",
            collectionGeneration = 6,
        )
        UsageAccessGuardRegistry.publish(current)

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
            UsageAccessGuardToken("process-a", 8),
            advanced,
        )
        assertEquals(advanced, UsageAccessGuardRegistry.snapshot())
    }
}
