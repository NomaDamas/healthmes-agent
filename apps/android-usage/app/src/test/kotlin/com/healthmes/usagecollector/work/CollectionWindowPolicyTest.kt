package com.healthmes.usagecollector.work

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CollectionWindowPolicyTest {

    @Test
    fun `permission revocation moves the readable boundary to now`() {
        val revokedAtMs = 1_785_556_321_000L

        val boundary = revokedPermissionBoundary(revokedAtMs)
        val queryBegin = activityQueryBegin(
            collectionSinceMs = boundary.collectionSinceMs,
            watermarkMs = boundary.watermarkMs,
            nowMs = revokedAtMs + 30 * 60 * 1000,
        )

        assertEquals(revokedAtMs, boundary.collectionSinceMs)
        assertTrue(boundary.watermarkMs <= revokedAtMs)
        assertTrue(queryBegin >= revokedAtMs)
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
}
