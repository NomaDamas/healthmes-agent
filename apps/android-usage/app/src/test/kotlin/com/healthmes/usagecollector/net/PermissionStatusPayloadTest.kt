package com.healthmes.usagecollector.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PermissionStatusPayloadTest {

    @Test
    fun `granted usage access advertises Android aggregate capability`() {
        val payload = permissionStatusPayload(
            granted = true,
            statusObservedAt = "2026-08-10T01:00:00Z",
            collectionGeneration = 12L,
        )

        assertEquals("android", payload.platform)
        assertEquals("aggregate", payload.capability)
        assertEquals("granted", payload.permissionStatus)
        assertNull(payload.statusReason)
        assertEquals("2026-08-10T01:00:00Z", payload.statusObservedAt)
        assertEquals(12L, payload.collectionGeneration)
        assertEquals(0, payload.queueDepth)
    }

    @Test
    fun `revoked usage access includes the fail closed reason`() {
        val payload = permissionStatusPayload(
            granted = false,
            statusObservedAt = "2026-08-10T01:05:00Z",
            collectionGeneration = 13L,
        )

        assertEquals("android", payload.platform)
        assertEquals("aggregate", payload.capability)
        assertEquals("revoked", payload.permissionStatus)
        assertEquals("usage_access_revoked", payload.statusReason)
        assertEquals("2026-08-10T01:05:00Z", payload.statusObservedAt)
        assertEquals(13L, payload.collectionGeneration)
        assertEquals(0, payload.queueDepth)
    }
}
