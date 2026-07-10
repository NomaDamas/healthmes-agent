package com.healthmes.companion

import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.GlanceBriefing
import java.time.Instant
import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Tests for the widget/tile state mapper: local-time rendering in the
 * server-reported user timezone, null-honesty, and drill-down URL choice.
 */
class BriefingDisplayStateTest {

    @Test
    fun `maps the populated payload to display state in the payload timezone`() {
        val briefing = GlanceBriefing.parse(Fixtures.full())

        // Explicit zone pin (same as the payload's) keeps the test hermetic.
        val state = BriefingDisplayState.from(briefing, ZoneId.of("Asia/Seoul"))

        assertEquals("72", state.scoreText)
        assertEquals(72, state.score)
        assertEquals("medium", state.confidence)
        // 05:00Z / 06:30Z -> 14:00 / 15:30 KST (UTC+9).
        assertEquals("14:00-15:30 Deep work: PLAN review", state.nextBlockLine)
        assertEquals("high", state.nextBlockDemand)
        assertEquals(2, state.alertCount)
        assertEquals("Stress spiked 45% above your 14-day baseline", state.alertSummary)
        assertEquals(Instant.parse("2026-07-09T05:23:11Z").toEpochMilli(), state.generatedAtMs)
    }

    @Test
    fun `uses the payload timezone when no override is given`() {
        val briefing = GlanceBriefing.parse(Fixtures.full())

        val state = BriefingDisplayState.from(briefing)

        // Payload says Asia/Seoul, so local wall-clock must be KST regardless
        // of the machine running this test.
        assertEquals("14:00-15:30 Deep work: PLAN review", state.nextBlockLine)
    }

    @Test
    fun `prefers the top alert decision url over the latest decision`() {
        val state = BriefingDisplayState.from(GlanceBriefing.parse(Fixtures.full()))

        assertEquals(
            "http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123",
            state.decisionUrl,
        )
    }

    @Test
    fun `falls back to the latest decision url when the top alert has none`() {
        val json = Fixtures.full().replace(
            "\"decision_url\": \"http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123\"",
            "\"decision_url\": null",
        )

        val state = BriefingDisplayState.from(GlanceBriefing.parse(json))

        assertEquals(
            "http://192.168.1.20:8100/decisions/7c9e6679-7425-40de-944b-e07fc1f90ae7?token=viewer-abc123",
            state.decisionUrl,
        )
    }

    @Test
    fun `maps the empty-database shape with honest placeholders`() {
        val state = BriefingDisplayState.from(GlanceBriefing.parse(Fixtures.empty()))

        assertEquals(BriefingDisplayState.NO_SCORE, state.scoreText)
        assertNull(state.score)
        assertEquals("low", state.confidence)
        assertNull(state.nextBlockLine)
        assertNull(state.nextBlockDemand)
        assertEquals(0, state.alertCount)
        assertNull(state.alertSummary)
        assertNull(state.decisionUrl)
    }

    @Test
    fun `renders untitled blocks with a placeholder label`() {
        // Drop the first (titled) block so the untitled one is next.
        val briefing = GlanceBriefing.parse(Fixtures.full())
        val untitledFirst = briefing.copy(nextBlocks = briefing.nextBlocks.drop(1))

        val state = BriefingDisplayState.from(untitledFirst, ZoneId.of("Asia/Seoul"))

        // 07:00Z-07:30Z -> 16:00-16:30 KST.
        assertEquals("16:00-16:30 (untitled block)", state.nextBlockLine)
        assertNull(state.nextBlockDemand)
    }

    @Test
    fun `unknown timezone falls back to the device zone without crashing`() {
        val briefing = GlanceBriefing.parse(
            Fixtures.full().replace("\"Asia/Seoul\"", "\"Not/AZone\"")
        )

        val state = BriefingDisplayState.from(briefing)

        assertNotNull(state.nextBlockLine)
        assertEquals("72", state.scoreText)
    }

    @Test
    fun `parses explicit-offset instants too`() {
        assertEquals(
            Instant.parse("2026-07-09T05:23:11Z"),
            BriefingDisplayState.parseIsoInstant("2026-07-09T05:23:11+00:00"),
        )
        assertEquals(
            Instant.parse("2026-07-09T05:23:11.123456Z"),
            BriefingDisplayState.parseIsoInstant("2026-07-09T05:23:11.123456Z"),
        )
    }
}
