package com.healthmes.usagecollector.usage

import com.healthmes.usagecollector.usage.AppForegroundEvent.Kind
import org.junit.Assert.assertEquals
import org.junit.Test

class SourcePrivacyFilterTest {

    @Test
    fun `excluded packages are removed before hourly bucketing`() {
        val events = listOf(
            AppForegroundEvent("com.private.App", "Main", 100L, Kind.RESUMED),
            AppForegroundEvent("com.private.App", "Main", 200L, Kind.PAUSED),
            AppForegroundEvent("com.allowed", "Main", 300L, Kind.RESUMED),
        )

        val filtered = SourcePrivacyFilter.filter(
            events,
            setOf(" COM.PRIVATE.APP "),
        )

        assertEquals(listOf("com.allowed"), filtered.map { it.packageName })
    }
}
