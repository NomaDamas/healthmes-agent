package com.healthmes.usagecollector

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CollectionStatePolicyTest {

    @Test
    fun `revoked observation returns the exact committed generation`() {
        val current = CollectionWindowState(
            collectionGeneration = 41L,
            collectionRevision = 3,
            collectionSinceMs = 1_000L,
            watermarkMs = 0L,
            collectionTimezone = "UTC",
            usageAccessGranted = true,
            usageSettingsPending = false,
        )
        val plan = permissionObservationPlan(
            current = current,
            granted = false,
            nowMs = 9_000L,
            boundaryWatermarkMs = 7_200L,
            timezone = "UTC",
            forceBoundary = false,
        )

        val committed = permissionObservationResult(
            plan = plan,
            persisted = true,
        )

        assertTrue(committed.persisted)
        assertTrue(committed.boundaryReset)
        assertEquals(true, committed.previousGranted)
        val committedState = checkNotNull(committed.committedState)
        assertEquals(42L, committedState.collectionGeneration)
        assertFalse(committedState.usageAccessGranted!!)
        assertEquals(9_000L, committedState.collectionSinceMs)

        val failed = permissionObservationResult(
            plan = plan,
            persisted = false,
        )
        assertFalse(failed.persisted)
        assertFalse(failed.boundaryReset)
        assertNull(failed.committedState)
    }

    @Test
    fun `install scoped device id does not reuse Android hardware identity`() {
        assertEquals(
            "android-install-123e4567e89b12d3a456426614174000",
            newInstallScopedDeviceId(
                "123e4567-e89b-12d3-a456-426614174000"
            ),
        )
    }

    @Test
    fun `generation mismatch recovery advances past both sides`() {
        assertEquals(
            43L,
            recoveryCollectionGeneration(
                localGeneration = 3L,
                serverGeneration = 42L,
            ),
        )
        assertEquals(
            43L,
            recoveryCollectionGeneration(
                localGeneration = 42L,
                serverGeneration = 3L,
            ),
        )
        assertNull(
            recoveryCollectionGeneration(
                localGeneration = Long.MAX_VALUE,
                serverGeneration = 3L,
            ),
        )
    }

    @Test(expected = IllegalArgumentException::class)
    fun `install scoped device id rejects weak random material`() {
        newInstallScopedDeviceId("android-id")
    }
}
