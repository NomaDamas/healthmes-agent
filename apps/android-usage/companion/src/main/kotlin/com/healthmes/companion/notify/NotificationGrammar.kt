package com.healthmes.companion.notify

import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.GlanceBriefing

/**
 * The docs/PLAN.md §8.5 notification grammar, as data:
 *
 * ```
 * [observation, 1 line]
 * [evidence, 1 line]
 * [proposal, 1 line]
 * [buttons]  Apply / Adjust / Keep as is   (stubs for now)
 * [link]     Why this? -> decision viewer URL
 * ```
 *
 * Pure Kotlin so the mapping from a glance payload is JVM unit-testable.
 *
 * PLACEHOLDER CONTENT: the glance endpoint only carries the alert summary +
 * counts, so evidence/proposal below are honest fillers proving the rendering
 * path. The real copy arrives with server-push alerts, and the wording rules
 * are the healthcare domain expert's deliverable
 * (docs/design/WATCH-NOTIFICATIONS.ko.md).
 */
data class NotificationGrammar(
    val observation: String,
    val evidence: String,
    val proposal: String,
    val decisionUrl: String?,
) {

    /** Three grammar lines for BigTextStyle. */
    fun bigText(): String = "$observation\n$evidence\n$proposal"

    companion object {

        /** Null when the payload has no top alert to phrase. */
        fun fromGlance(briefing: GlanceBriefing): NotificationGrammar? {
            val top = briefing.alerts.top ?: return null
            val score = briefing.energy.score?.toString() ?: BriefingDisplayState.NO_SCORE
            val evidence =
                "Rule ${top.ruleId} fired · ${briefing.alerts.unresolvedCount} unresolved " +
                    "in 24h · energy $score (${briefing.energy.confidence})"
            val proposal =
                if (top.decisionUrl != null) {
                    "Open the decision record for the reasoning; reply in Telegram to adjust."
                } else {
                    "Reply in Telegram to review and adjust today's plan."
                }
            return NotificationGrammar(
                observation = top.summary,
                evidence = evidence,
                proposal = proposal,
                decisionUrl = top.decisionUrl,
            )
        }
    }
}
