package com.healthmes.companion

import com.healthmes.briefing.GlanceBriefing
import org.json.JSONException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Contract tests for the `GET /v1/briefing/glance` parser against fixture
 * payloads that mirror healthmes/api/briefing.py verbatim.
 */
class GlanceBriefingParserTest {

    @Test
    fun `parses top-level fields of a populated payload`() {
        val briefing = GlanceBriefing.parse(Fixtures.full())

        assertEquals("2026-07-09T05:23:11Z", briefing.generatedAtIso)
        assertEquals("Asia/Seoul", briefing.timezone)
    }

    @Test
    fun `parses energy score, confidence, and the 24-entry curve`() {
        val energy = GlanceBriefing.parse(Fixtures.full()).energy

        assertEquals(72, energy.score)
        assertEquals("medium", energy.confidence)
        assertEquals(24, energy.curve24h.size)
        // Hours ascending 0..23, exactly as the contract promises.
        assertEquals((0..23).toList(), energy.curve24h.map { it.hour })
        // Missing hours are honest nulls; persisted hours carry scores.
        assertNull(energy.curve24h[0].score)
        assertEquals(64, energy.curve24h[7].score)
        assertEquals(78, energy.curve24h[10].score)
        assertEquals(72, energy.curve24h[14].score)
        assertNull(energy.curve24h[23].score)
    }

    @Test
    fun `parses next blocks in order with nullable title and demand`() {
        val blocks = GlanceBriefing.parse(Fixtures.full()).nextBlocks

        assertEquals(3, blocks.size)

        assertEquals("2026-07-09T05:00:00Z", blocks[0].startIso)
        assertEquals("2026-07-09T06:30:00Z", blocks[0].endIso)
        assertEquals("Deep work: PLAN review", blocks[0].title)
        assertEquals("high", blocks[0].energyDemand)
        assertEquals("calendar", blocks[0].source)

        assertNull(blocks[1].title)
        assertNull(blocks[1].energyDemand)
        assertEquals("calendar", blocks[1].source)

        assertEquals("Stretch break", blocks[2].title)
        assertEquals("low", blocks[2].energyDemand)
        assertEquals("proposal", blocks[2].source)
    }

    @Test
    fun `parses alerts digest and tokenized decision urls`() {
        val briefing = GlanceBriefing.parse(Fixtures.full())

        assertEquals(2, briefing.alerts.unresolvedCount)
        val top = checkNotNull(briefing.alerts.top)
        assertEquals("stress_spike", top.ruleId)
        assertEquals("Stress spiked 45% above your 14-day baseline", top.summary)
        assertEquals(
            "http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123",
            top.decisionUrl,
        )

        val latest = checkNotNull(briefing.latestDecision)
        assertEquals("7c9e6679-7425-40de-944b-e07fc1f90ae7", latest.id)
        assertTrue(latest.url.endsWith("?token=viewer-abc123"))
    }

    @Test
    fun `parses the empty-database shape`() {
        val briefing = GlanceBriefing.parse(Fixtures.empty())

        assertNull(briefing.energy.score)
        assertEquals("low", briefing.energy.confidence)
        assertEquals(24, briefing.energy.curve24h.size)
        assertTrue(briefing.energy.curve24h.all { it.score == null })
        assertTrue(briefing.nextBlocks.isEmpty())
        assertEquals(0, briefing.alerts.unresolvedCount)
        assertNull(briefing.alerts.top)
        assertNull(briefing.latestDecision)
    }

    @Test
    fun `null top decision_url stays null`() {
        val json = Fixtures.full().replace(
            "\"decision_url\": \"http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123\"",
            "\"decision_url\": null",
        )

        val top = checkNotNull(GlanceBriefing.parse(json).alerts.top)

        assertEquals("stress_spike", top.ruleId)
        assertNull(top.decisionUrl)
    }

    @Test
    fun `unknown extra keys are ignored (additive server evolution)`() {
        val json = Fixtures.full().replaceFirst(
            "\"generated_at\"",
            "\"future_field\": {\"nested\": [1, 2, 3]}, \"generated_at\"",
        )

        val briefing = GlanceBriefing.parse(json)

        assertEquals(72, briefing.energy.score)
    }

    @Test
    fun `malformed json throws`() {
        assertThrows(JSONException::class.java) { GlanceBriefing.parse("not json at all") }
    }

    @Test
    fun `missing required key throws`() {
        val json = Fixtures.full().replace("\"unresolved_count\"", "\"renamed_count\"")

        assertThrows(JSONException::class.java) { GlanceBriefing.parse(json) }
    }
}
