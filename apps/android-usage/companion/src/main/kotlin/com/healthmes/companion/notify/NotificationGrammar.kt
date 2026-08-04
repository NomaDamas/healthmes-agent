package com.healthmes.companion.notify

import com.healthmes.api.AlertItem
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.GlanceBriefing

/**
 * The docs/PLAN.md §8.5 notification grammar, as data:
 *
 * ```
 * [observation, 1 line]
 * [evidence, 1 line]
 * [proposal, 1 line]
 * [buttons]  Apply / Adjust / Keep as is   (real schedule-proposal actions)
 * [link]     Why this? -> decision viewer URL
 * ```
 *
 * Pure Kotlin so the mapping from server payloads is JVM unit-testable.
 *
 * Two sources, in preference order:
 * - [fromAlert] — a `GET /v1/alerts` item carrying the REAL grammar lines the
 *   trigger recorded at fire time (observation `summary`, `evidence` facts,
 *   `proposal`).
 * - [fromGlance] — fallback from the glance payload alone (only the
 *   observation is real there; evidence/proposal are honest fillers proving
 *   the rendering path). Wording rules stay the healthcare domain expert's
 *   deliverable (docs/design/WATCH-NOTIFICATIONS.ko.md).
 */
data class NotificationGrammar(
    val alertId: String?,
    val alertRuleId: String,
    val observation: String,
    val evidence: String,
    val proposal: String,
    val decisionUrl: String?,
    val proposalId: String?,
    val alertRevision: String?,
) {

    /** Three grammar lines for BigTextStyle. */
    fun bigText(): String = "$observation\n$evidence\n$proposal"

    companion object {

        /** Real grammar lines from an alert-history item (issue #10). */
        fun fromAlert(alert: AlertItem): NotificationGrammar = NotificationGrammar(
            alertId = alert.id,
            alertRuleId = alert.ruleId,
            observation = alert.summary,
            evidence = alert.evidenceLine() ?: "Rule ${alert.ruleId} fired",
            proposal = alert.proposal
                ?: "Open the decision record for the reasoning, or adjust in the app.",
            decisionUrl = alert.decisionUrl,
            proposalId = alert.proposalId,
            alertRevision = "${alert.id}:${alert.proposalId ?: "informational"}",
        )

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
                alertId = top.id,
                alertRuleId = top.ruleId,
                observation = top.summary,
                evidence = evidence,
                proposal = proposal,
                decisionUrl = top.decisionUrl,
                proposalId = null,
                alertRevision = top.id?.let { "$it:informational" },
            )
        }

        fun pendingNotification(
            previousRevisions: Set<String>,
            alerts: List<NotificationGrammar>,
        ): NotificationGrammar? = alerts.firstOrNull { alert ->
            val id = alert.alertId
            val revision = alert.alertRevision
            if (id == null || revision == null || revision in previousRevisions) {
                false
            } else {
                val hasPriorRevision = previousRevisions.any { it.startsWith("$id:") }
                !hasPriorRevision || alert.proposalId != null
            }
        }

        fun legacyRevisions(alerts: List<NotificationGrammar>): Set<String> =
            alerts.mapNotNull { alert ->
                alert.alertId?.let { "$it:informational" }
            }.toSet()

        fun currentRevisions(alerts: List<NotificationGrammar>): Set<String> =
            alerts.mapNotNull { it.alertRevision }.toSet()

        fun mergeRevisions(
            previousRevisions: Set<String>,
            alerts: List<NotificationGrammar>,
            notifiedRevision: String?,
        ): Set<String> {
            val currentIds = alerts.mapNotNull { it.alertId }.toSet()
            return buildSet {
                addAll(previousRevisions.filter { it.substringBefore(':') in currentIds })
                for (alert in alerts) {
                    val revision = alert.alertRevision ?: continue
                    if (
                        revision in previousRevisions ||
                        revision == notifiedRevision
                    ) {
                        add(revision)
                    }
                }
            }
        }
    }
}
