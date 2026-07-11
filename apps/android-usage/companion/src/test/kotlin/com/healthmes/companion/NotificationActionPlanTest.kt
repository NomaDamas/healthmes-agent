package com.healthmes.companion

import com.healthmes.api.AlertsPage
import com.healthmes.companion.notify.NotificationActionPlan
import com.healthmes.companion.notify.NotificationGrammar
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Intent-building plan for the §8.5 notification buttons: ✅→accept,
 * ❌ keep-as-is→decline, ✏️→proposals screen, content tap→decision viewer.
 */
class NotificationActionPlanTest {

    private fun grammarFromFixture(index: Int = 0): NotificationGrammar =
        NotificationGrammar.fromAlert(
            AlertsPage.parse(Fixtures.load("alerts_page.json")).alerts[index]
        )

    @Test
    fun `apply and keep-as-is map to the real wire actions`() {
        val plan = NotificationActionPlan.from(grammarFromFixture())

        assertEquals(NotificationActionPlan.WIRE_ACCEPT, plan.accept.wireAction)
        assertEquals(NotificationActionPlan.WIRE_DECLINE, plan.decline.wireAction)
        // No alert→proposal linkage exists server-side yet: the worker
        // resolves the target at tap time.
        assertNull(plan.accept.proposalId)
        assertNull(plan.decline.proposalId)
    }

    @Test
    fun `every pending-intent request code in the app is distinct`() {
        // Intent.filterEquals ignores extras, so ANY two activity
        // PendingIntents sharing a request code alias to one record and
        // FLAG_UPDATE_CURRENT lets one notifier clobber another's extras
        // (e.g. the focus-block poll erasing an alert's decision deep link).
        // The registry on NotificationActionPlan must stay collision-free.
        val plan = NotificationActionPlan.from(grammarFromFixture())
        val codes = setOf(
            plan.accept.requestCode,
            plan.decline.requestCode,
            NotificationActionPlan.REQUEST_ADJUST,
            NotificationActionPlan.REQUEST_ALERT_CONTENT_TAP,
            NotificationActionPlan.REQUEST_FOCUS_BLOCK_TAP,
            NotificationActionPlan.REQUEST_ACTION_RESULT_TAP,
        )

        assertEquals(6, codes.size)
    }

    @Test
    fun `adjust deep-links into the proposals screen`() {
        assertEquals(
            NotificationActionPlan.DEST_PROPOSALS,
            NotificationActionPlan.from(grammarFromFixture()).adjustDestination,
        )
    }

    @Test
    fun `content tap opens the decision viewer when the alert has a link`() {
        val plan = NotificationActionPlan.from(grammarFromFixture(0))

        assertEquals(
            NotificationActionPlan.ContentTap.Decision(
                "http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123"
            ),
            plan.contentTap,
        )
    }

    @Test
    fun `content tap falls back to home without a decision link`() {
        val plan = NotificationActionPlan.from(grammarFromFixture(1))

        assertEquals(NotificationActionPlan.ContentTap.Home, plan.contentTap)
    }

    @Test
    fun `real alert grammar prefers fire-time lines`() {
        val grammar = grammarFromFixture(0)

        assertEquals("Stress spiked 45% above your 14-day baseline", grammar.observation)
        // Evidence is the recorded facts, not the glance-derived filler.
        assertTrue(grammar.evidence.contains("hrv_delta_pct: -18"))
        assertEquals(
            "Move the 14:00 deep-work block to tomorrow morning and keep the afternoon light.",
            grammar.proposal,
        )
    }

    @Test
    fun `legacy alert grammar still renders all three lines`() {
        val grammar = grammarFromFixture(1)

        assertEquals("schedule_changed", grammar.observation)
        assertEquals("Rule schedule_changed fired", grammar.evidence)
        assertTrue(grammar.proposal.isNotBlank())
        assertNull(grammar.decisionUrl)
    }
}
