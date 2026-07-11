package com.healthmes.companion.notify

import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.GlanceBriefing

/**
 * Pure "which block is running right now?" logic behind the ongoing
 * focus-block notification (JVM unit-tested; [FocusBlockNotifier] is the
 * Android shell).
 */
object FocusBlockLogic {

    /**
     * The block covering [nowMs] (start ≤ now < end); when blocks overlap
     * the one ending soonest wins (its countdown is the actionable one).
     * Null when nothing is active — the notification must be absent then.
     */
    fun activeBlock(blocks: List<GlanceBriefing.Block>, nowMs: Long): GlanceBriefing.Block? =
        blocks
            .filter { block ->
                val start = BriefingDisplayState.parseIsoInstant(block.startIso).toEpochMilli()
                val end = BriefingDisplayState.parseIsoInstant(block.endIso).toEpochMilli()
                nowMs in start until end
            }
            .minByOrNull { BriefingDisplayState.parseIsoInstant(it.endIso).toEpochMilli() }
}
