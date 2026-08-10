package com.healthmes.usagecollector.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ServerUrlPolicyTest {

    @Test
    fun `https base URLs are normalized`() {
        assertEquals(
            "https://healthmes.example:8100",
            normalizedSecureServerUrl(" https://healthmes.example:8100/ "),
        )
    }

    @Test
    fun `cleartext and credential-bearing URLs are rejected`() {
        listOf(
            "http://192.168.1.20:8100",
            "https://token@healthmes.example",
            "https://healthmes.example/path",
            "https://healthmes.example?token=secret",
            "ftp://healthmes.example",
            "not-a-url",
        ).forEach { value ->
            assertNull(value, normalizedSecureServerUrl(value))
        }
    }
}
