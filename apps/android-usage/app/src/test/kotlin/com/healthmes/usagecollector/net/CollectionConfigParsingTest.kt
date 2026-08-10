package com.healthmes.usagecollector.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class CollectionConfigParsingTest {

    @Test
    fun `valid collection config preserves excluded apps`() {
        val config = parseCollectionConfig(
            """
            {
              "enabled": true,
              "effective_collecting": true,
              "blocked_reason": null,
              "excluded_apps": ["private.app", "bank.app"],
              "config_revision": 7
            }
            """.trimIndent(),
        )

        assertEquals(setOf("private.app", "bank.app"), config.excludedApps)
        assertEquals(7, config.configRevision)
        assertNull(config.blockedReason)
    }

    @Test
    fun `missing excluded apps fails closed`() {
        assertThrows(IllegalArgumentException::class.java) {
            parseCollectionConfig(
                """
                {
                  "enabled": true,
                  "effective_collecting": true,
                  "blocked_reason": null,
                  "config_revision": 7
                }
                """.trimIndent(),
            )
        }
    }

    @Test
    fun `wrongly typed excluded apps fail closed`() {
        for (value in listOf("null", "\"private.app\"", "[\"private.app\", 7]")) {
            assertThrows(IllegalArgumentException::class.java) {
                parseCollectionConfig(
                    """
                    {
                      "enabled": true,
                      "effective_collecting": true,
                      "blocked_reason": null,
                      "excluded_apps": $value,
                      "config_revision": 7
                    }
                    """.trimIndent(),
                )
            }
        }
    }

    @Test
    fun `wrongly typed or out of range revision fails closed`() {
        for (value in listOf("\"7\"", "7.0", "true", "-1", "2147483648")) {
            assertThrows(IllegalArgumentException::class.java) {
                parseCollectionConfig(
                    """
                    {
                      "enabled": true,
                      "effective_collecting": true,
                      "blocked_reason": null,
                      "excluded_apps": [],
                      "config_revision": $value
                    }
                    """.trimIndent(),
                )
            }
        }
    }
}
