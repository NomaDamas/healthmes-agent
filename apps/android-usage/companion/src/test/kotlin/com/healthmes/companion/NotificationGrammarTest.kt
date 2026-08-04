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
    fun `glance fallback carries the exact informational alert revision`() {
        val alertId = "00000000-0000-0000-0000-00000000a123"
        val grammar = checkNotNull(
            NotificationGrammar.fromGlance(GlanceBriefing.parse(Fixtures.full()))
        )

        assertEquals(alertId, grammar.alertId)
        assertEquals("$alertId:informational", grammar.alertRevision)
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

    @Test
    fun `finds an actionable upgrade even when it is no longer newest`() {
        val page = com.healthmes.api.AlertsPage.parse(Fixtures.load("alerts_page.json"))
        val actionable = NotificationGrammar.fromAlert(page.alerts[0])
        val newer = NotificationGrammar.fromAlert(page.alerts[1])
        val previous = setOf("${actionable.alertId}:informational", newer.alertRevision!!)

        assertEquals(
            actionable,
            NotificationGrammar.pendingNotification(previous, listOf(newer, actionable)),
        )
    }

    @Test
    fun `informational downgrade keeps the actionable revision observed`() {
        val page = com.healthmes.api.AlertsPage.parse(Fixtures.load("alerts_page.json"))
        val actionable = NotificationGrammar.fromAlert(page.alerts[0])
        val informational = actionable.copy(
            proposalId = null,
            alertRevision = "${actionable.alertId}:informational",
        )
        val previous = setOf(actionable.alertRevision!!)
        val merged = NotificationGrammar.mergeRevisions(previous, listOf(informational), null)

        assertTrue(actionable.alertRevision in merged)
        assertNull(NotificationGrammar.pendingNotification(merged, listOf(informational)))
    }

    @Test
    fun `unnotified simultaneous upgrade remains pending for the next poll`() {
        val page = com.healthmes.api.AlertsPage.parse(Fixtures.load("alerts_page.json"))
        val first = NotificationGrammar.fromAlert(page.alerts[0])
        val second = first.copy(
            alertId = "another-alert",
            alertRevision = "another-alert:another-proposal",
        )
        val previous = setOf(
            "${first.alertId}:informational",
            "${second.alertId}:informational",
        )
        val merged = NotificationGrammar.mergeRevisions(
            previous,
            listOf(first, second),
            first.alertRevision,
        )

        assertTrue(first.alertRevision in merged)
        assertTrue(second.alertRevision !in merged)
        assertEquals(
            second,
            NotificationGrammar.pendingNotification(merged, listOf(first, second)),
        )
    }

    @Test
    fun `legacy notification state treats current actions as unseen upgrades`() {
        val page = com.healthmes.api.AlertsPage.parse(Fixtures.load("alerts_page.json"))
        val actionable = NotificationGrammar.fromAlert(page.alerts[0])
        val previous = NotificationGrammar.legacyRevisions(listOf(actionable))

        assertEquals(setOf("${actionable.alertId}:informational"), previous)
        assertEquals(
            actionable,
            NotificationGrammar.pendingNotification(previous, listOf(actionable)),
        )
    }

    @Test
    fun `new alert remains notifyable when the window count stays constant`() {
        val page = com.healthmes.api.AlertsPage.parse(Fixtures.load("alerts_page.json"))
        val expired = NotificationGrammar.fromAlert(page.alerts[1])
        val replacement = NotificationGrammar.fromAlert(page.alerts[0])
        val previous = setOf(expired.alertRevision!!)

        assertEquals(
            replacement,
            NotificationGrammar.pendingNotification(previous, listOf(replacement)),
        )
        assertTrue(
            replacement.alertRevision !in
                NotificationGrammar.mergeRevisions(previous, listOf(replacement), null),
        )
    }

    @Test
    fun `fresh baseline records current revisions without notifications`() {
        val page = com.healthmes.api.AlertsPage.parse(Fixtures.load("alerts_page.json"))
        val alerts = page.alerts.map { NotificationGrammar.fromAlert(it) }
        val baseline = NotificationGrammar.currentRevisions(alerts)

        assertEquals(alerts.mapNotNull { it.alertRevision }.toSet(), baseline)
        assertNull(NotificationGrammar.pendingNotification(baseline, alerts))
    }

    @Test
    fun `feed recovery tracks the exact fallback alert when summaries collide`() {
        val page = com.healthmes.api.AlertsPage.parse(Fixtures.load("alerts_page.json"))
        val detailedAlert = NotificationGrammar.fromAlert(page.alerts[0])
        val fallbackAlert = detailedAlert.copy(
            proposalId = null,
            alertRevision = "${detailedAlert.alertId}:informational",
        )
        val unseenReplacement = fallbackAlert.copy(
            alertId = "duplicate-summary-alert",
            alertRevision = "duplicate-summary-alert:informational",
        )
        val recovered = setOf(checkNotNull(fallbackAlert.alertRevision))

        assertEquals(setOf("${fallbackAlert.alertId}:informational"), recovered)
        assertEquals(
            unseenReplacement,
            NotificationGrammar.pendingNotification(recovered, listOf(unseenReplacement)),
        )
    }
}
