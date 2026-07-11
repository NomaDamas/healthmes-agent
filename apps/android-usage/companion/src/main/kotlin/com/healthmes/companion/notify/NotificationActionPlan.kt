package com.healthmes.companion.notify

/**
 * Pure description of the intents an alert notification wires up — extracted
 * from [AlertNotifier] so the mapping is JVM unit-testable (building real
 * `Intent`s needs a device/Robolectric):
 *
 * - ✅ Apply → broadcast → one-shot worker → `POST …/proposals/{id}/accept`
 * - ❌ Keep as is → broadcast → same worker → `POST …/proposals/{id}/decline`
 *   (§8.5: "keep as is today" = decline the pending schedule proposal)
 * - ✏️ Adjust → opens the app's proposals screen (choose/adjust in-app)
 * - content tap → the decision viewer for the alert's `decision_url`,
 *   or the briefing home when the alert carries none
 *
 * The §8.5 grammar has no proposal-id linkage yet (alerts are trigger
 * events; the proposal is created by the planner), so [proposalId] stays
 * null and the worker resolves the target pending proposal at tap time —
 * acting ONLY when it is unambiguous (see ProposalActionLogic).
 */
data class NotificationActionPlan(
    val accept: ActionSpec,
    val decline: ActionSpec,
    /** Adjust button → in-app destination (proposals screen). */
    val adjustDestination: String,
    /** Content tap target. */
    val contentTap: ContentTap,
) {

    /** One broadcast-backed button: wire action + a stable request code. */
    data class ActionSpec(
        /** Wire action sent to the worker: "accept" | "decline". */
        val wireAction: String,
        /** Explicit proposal id, or null = resolve-at-tap-time. */
        val proposalId: String?,
        /** PendingIntent request code — must differ across the buttons. */
        val requestCode: Int,
    )

    sealed class ContentTap {
        /** Open the in-app decision viewer on this tokenized URL. */
        data class Decision(val url: String) : ContentTap()

        /** No decision link — open the briefing home. */
        data object Home : ContentTap()
    }

    companion object {
        const val WIRE_ACCEPT = "accept"
        const val WIRE_DECLINE = "decline"
        const val DEST_PROPOSALS = "proposals"

        const val REQUEST_ACCEPT = 1
        const val REQUEST_ADJUST = 2
        const val REQUEST_DECLINE = 3

        /**
         * Content-tap request codes — one per notifier, registry-style.
         *
         * `Intent.filterEquals` ignores extras, and every content tap in the
         * app targets MainActivity differing only in extras. With a shared
         * request code they would all resolve to ONE PendingIntent record,
         * and `FLAG_UPDATE_CURRENT` would let each `notify()` clobber the
         * others' extras — e.g. the ongoing focus-block update (no extras)
         * silently erasing a §8.5 alert's decision deep link, dropping the
         * "왜 이 판단?" tap-through that WATCH-NOTIFICATIONS.ko.md §1.1 says
         * no surface may drop. Every activity PendingIntent in the app must
         * take a distinct code from this registry.
         */
        const val REQUEST_ALERT_CONTENT_TAP = 4
        const val REQUEST_FOCUS_BLOCK_TAP = 5
        const val REQUEST_ACTION_RESULT_TAP = 6

        fun from(grammar: NotificationGrammar, proposalId: String? = null) =
            NotificationActionPlan(
                accept = ActionSpec(WIRE_ACCEPT, proposalId, REQUEST_ACCEPT),
                decline = ActionSpec(WIRE_DECLINE, proposalId, REQUEST_DECLINE),
                adjustDestination = DEST_PROPOSALS,
                contentTap = grammar.decisionUrl?.let { ContentTap.Decision(it) }
                    ?: ContentTap.Home,
            )
    }
}
