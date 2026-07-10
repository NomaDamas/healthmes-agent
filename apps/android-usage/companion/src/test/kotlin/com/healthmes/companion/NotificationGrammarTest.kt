package com.healthmes.companion

import com.healthmes.briefing.GlanceBriefing
import com.healthmes.companion.notify.NotificationGrammar
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Tests for the PLAN.md §8.5 grammar mapping (placeholder copy included). */
class NotificationGrammarTest {

    @Test
    fun `phrases the top alert as observation-evidence-proposal`() {
        val grammar = checkNotNull(
            NotificationGrammar.fromGlance(GlanceBriefing.parse(Fixtures.full()))
        )

        assertEquals("Stress spiked 45% above your 14-day baseline", grammar.observation)
        assertTrue(grammar.evidence.contains("stress_spike"))
        assertTrue(grammar.evidence.contains("2 unresolved"))
        assertTrue(grammar.evidence.contains("energy 72 (medium)"))
        assertTrue(grammar.proposal.contains("decision record"))
        assertEquals(
            "http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123",
            grammar.decisionUrl,
        )
        assertEquals(
            listOf(grammar.observation, grammar.evidence, grammar.proposal),
            grammar.bigText().split("\n"),
        )
    }

    @Test
    fun `no top alert means nothing to phrase`() {
        assertNull(NotificationGrammar.fromGlance(GlanceBriefing.parse(Fixtures.empty())))
    }

    @Test
    fun `proposal adapts when the alert has no decision url`() {
        val json = Fixtures.full().replace(
            "\"decision_url\": \"http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123\"",
            "\"decision_url\": null",
        )

        val grammar = checkNotNull(NotificationGrammar.fromGlance(GlanceBriefing.parse(json)))

        assertNull(grammar.decisionUrl)
        assertTrue(grammar.proposal.contains("Telegram"))
    }
}
