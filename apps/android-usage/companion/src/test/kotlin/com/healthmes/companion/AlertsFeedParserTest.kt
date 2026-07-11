package com.healthmes.companion

import com.healthmes.api.AlertsPage
import com.healthmes.briefing.GlanceBriefing
import org.json.JSONException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the `GET /v1/alerts` page contract (healthmes/api/alerts.py) the app's
 * alert list and notification content build on.
 */
class AlertsFeedParserTest {

    @Test
    fun `parses the full page with grammar lines and pagination`() {
        val page = AlertsPage.parse(Fixtures.load("alerts_page.json"))

        assertEquals(2, page.alerts.size)
        val top = page.alerts.first()
        assertEquals("3f6a1c2e-8d4b-4f0a-9c7e-5b2d1a0f9e8d", top.id)
        assertEquals("stress_spike", top.ruleId)
        assertEquals("2026-07-09T04:55:00Z", top.firedAtIso)
        assertEquals("Stress spiked 45% above your 14-day baseline", top.summary)
        assertEquals(
            "Move the 14:00 deep-work block to tomorrow morning and keep the afternoon light.",
            top.proposal,
        )
        assertEquals(
            mapOf("hrv_delta_pct" to "-18", "window_hours" to "24", "baseline" to "14-day"),
            top.evidence,
        )
        assertEquals(
            "http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123",
            top.decisionUrl,
        )

        assertEquals(2, page.pagination.totalCount)
        assertEquals(50, page.pagination.limit)
        assertEquals(0, page.pagination.offset)
        assertFalse(page.pagination.hasMore)
    }

    @Test
    fun `top alert agrees with the glance top alert`() {
        // Server contract: alerts[0] and glance alerts.top use the same
        // heuristic — the fixtures pin that agreement client-side too.
        val top = AlertsPage.parse(Fixtures.load("alerts_page.json")).alerts.first()
        val glanceTop = checkNotNull(GlanceBriefing.parse(Fixtures.full()).alerts.top)

        assertEquals(glanceTop.ruleId, top.ruleId)
        assertEquals(glanceTop.summary, top.summary)
        assertEquals(glanceTop.decisionUrl, top.decisionUrl)
    }

    @Test
    fun `legacy rows fall back honestly`() {
        val legacy = AlertsPage.parse(Fixtures.load("alerts_page.json")).alerts[1]

        // Payload-less legacy rows: summary falls back to rule_id server-side.
        assertEquals(legacy.ruleId, legacy.summary)
        assertNull(legacy.proposal)
        assertTrue(legacy.evidence.isEmpty())
        assertNull(legacy.evidenceLine())
        assertNull(legacy.decisionUrl)
    }

    @Test
    fun `evidence renders as one compact line`() {
        val top = AlertsPage.parse(Fixtures.load("alerts_page.json")).alerts.first()
        val line = checkNotNull(top.evidenceLine())

        assertTrue(line.contains("hrv_delta_pct: -18"))
        assertTrue(line.contains(" · "))
    }

    @Test
    fun `unknown keys are ignored`() {
        val withExtras = Fixtures.load("alerts_page.json").replace(
            "\"rule_id\": \"stress_spike\",",
            "\"rule_id\": \"stress_spike\", \"severity\": \"future-field\",",
        )

        assertEquals("stress_spike", AlertsPage.parse(withExtras).alerts.first().ruleId)
    }

    @Test(expected = JSONException::class)
    fun `missing required key throws`() {
        AlertsPage.parse(
            Fixtures.load("alerts_page.json").replace("\"rule_id\": \"stress_spike\",", "")
        )
    }
}
