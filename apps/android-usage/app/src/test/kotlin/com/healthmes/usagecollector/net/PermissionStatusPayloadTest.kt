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
            pairingRevision = 3L,
        )

        assertEquals("android", payload.platform)
        assertEquals("aggregate", payload.capability)
        assertEquals("granted", payload.permissionStatus)
        assertNull(payload.statusReason)
        assertEquals("2026-08-10T01:00:00Z", payload.statusObservedAt)
        assertEquals(12L, payload.collectionGeneration)
        assertEquals(3L, payload.pairingRevision)
        assertEquals(0, payload.queueDepth)
    }

    @Test
    fun `revoked usage access includes the fail closed reason`() {
        val payload = permissionStatusPayload(
            granted = false,
            statusObservedAt = "2026-08-10T01:05:00Z",
            collectionGeneration = 13L,
            pairingRevision = 4L,
        )

        assertEquals("android", payload.platform)
        assertEquals("aggregate", payload.capability)
        assertEquals("revoked", payload.permissionStatus)
        assertEquals("usage_access_revoked", payload.statusReason)
        assertEquals("2026-08-10T01:05:00Z", payload.statusObservedAt)
        assertEquals(13L, payload.collectionGeneration)
        assertEquals(4L, payload.pairingRevision)
        assertEquals(0, payload.queueDepth)
    }

    @Test
    fun `pairing boundary closure uses an explicit non permission reason`() {
        val payload = permissionStatusPayload(
            granted = false,
            statusObservedAt = "2026-08-10T01:10:00Z",
            collectionGeneration = 14L,
            pairingRevision = 5L,
            statusReason = "local_collection_boundary_changed",
        )

        assertEquals("revoked", payload.permissionStatus)
        assertEquals(
            "local_collection_boundary_changed",
            payload.statusReason,
        )
    }
}
