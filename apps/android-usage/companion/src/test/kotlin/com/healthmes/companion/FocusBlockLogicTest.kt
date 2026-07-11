package com.healthmes.companion

import com.healthmes.briefing.GlanceBriefing
import com.healthmes.companion.notify.FocusBlockLogic
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Ongoing focus-block selection: strict [start, end) and no guessing. */
class FocusBlockLogicTest {

    private fun block(start: String, end: String, title: String) =
        GlanceBriefing.Block(
            startIso = start,
            endIso = end,
            title = title,
            energyDemand = null,
            source = "calendar",
        )

    private fun ms(iso: String) = Instant.parse(iso).toEpochMilli()

    @Test
    fun `no block active before the first start`() {
        val blocks = listOf(block("2026-07-09T05:00:00Z", "2026-07-09T06:30:00Z", "Deep work"))

        assertNull(FocusBlockLogic.activeBlock(blocks, ms("2026-07-09T04:59:59Z")))
    }

    @Test
    fun `the covering block is active, start inclusive end exclusive`() {
        val blocks = listOf(block("2026-07-09T05:00:00Z", "2026-07-09T06:30:00Z", "Deep work"))

        assertEquals(
            "Deep work",
            FocusBlockLogic.activeBlock(blocks, ms("2026-07-09T05:00:00Z"))?.title,
        )
        assertEquals(
            "Deep work",
            FocusBlockLogic.activeBlock(blocks, ms("2026-07-09T06:29:59Z"))?.title,
        )
        assertNull(FocusBlockLogic.activeBlock(blocks, ms("2026-07-09T06:30:00Z")))
    }

    @Test
    fun `overlapping blocks pick the one ending soonest`() {
        val blocks = listOf(
            block("2026-07-09T05:00:00Z", "2026-07-09T08:00:00Z", "Long block"),
            block("2026-07-09T05:30:00Z", "2026-07-09T06:00:00Z", "Short block"),
        )

        assertEquals(
            "Short block",
            FocusBlockLogic.activeBlock(blocks, ms("2026-07-09T05:45:00Z"))?.title,
        )
    }

    @Test
    fun `glance fixture blocks activate at their times`() {
        val briefing = GlanceBriefing.parse(Fixtures.full())

        // 05:00–06:30Z "Deep work: PLAN review" is active at 05:23Z.
        assertEquals(
            "Deep work: PLAN review",
            FocusBlockLogic.activeBlock(briefing.nextBlocks, ms("2026-07-09T05:23:11Z"))?.title,
        )
        // Gap between blocks: nothing active.
        assertNull(FocusBlockLogic.activeBlock(briefing.nextBlocks, ms("2026-07-09T06:45:00Z")))
    }
}
