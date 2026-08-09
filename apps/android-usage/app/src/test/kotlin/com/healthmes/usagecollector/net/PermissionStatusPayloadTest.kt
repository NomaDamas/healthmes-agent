package com.healthmes.usagecollector.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PermissionStatusPayloadTest {

    @Test
    fun `granted usage access advertises Android aggregate capability`() {
        val payload = permissionStatusPayload(granted = true)

        assertEquals("android", payload.platform)
        assertEquals("aggregate", payload.capability)
        assertEquals("granted", payload.permissionStatus)
        assertNull(payload.statusReason)
        assertEquals(0, payload.queueDepth)
    }

    @Test
    fun `revoked usage access includes the fail closed reason`() {
        val payload = permissionStatusPayload(granted = false)

        assertEquals("android", payload.platform)
        assertEquals("aggregate", payload.capability)
        assertEquals("revoked", payload.permissionStatus)
        assertEquals("usage_access_revoked", payload.statusReason)
        assertEquals(0, payload.queueDepth)
    }
}
