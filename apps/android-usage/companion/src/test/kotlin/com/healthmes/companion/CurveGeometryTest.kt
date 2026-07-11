package com.healthmes.companion

import com.healthmes.briefing.GlanceBriefing
import com.healthmes.companion.ui.CurveGeometry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The 24h-curve geometry honors the glance contract's honest nulls: missing
 * hours become real gaps (separate polyline runs), never interpolation.
 */
class CurveGeometryTest {

    private fun point(hour: Int, score: Int?) = GlanceBriefing.CurvePoint(hour, score)

    @Test
    fun `all-null curve renders nothing`() {
        val segments = CurveGeometry.segments((0..23).map { point(it, null) })

        assertTrue(segments.isEmpty())
    }

    @Test
    fun `null hours split the curve into runs`() {
        val curve = listOf(
            point(0, 50), point(1, 60),
            point(2, null),
            point(3, 70), point(4, 80), point(5, 90),
        )

        val segments = CurveGeometry.segments(curve)

        assertEquals(2, segments.size)
        assertEquals(2, segments[0].size)
        assertEquals(3, segments[1].size)
    }

    @Test
    fun `isolated hours become single-point runs`() {
        val curve = listOf(point(0, null), point(1, 42), point(2, null))

        val segments = CurveGeometry.segments(curve)

        assertEquals(1, segments.size)
        assertEquals(1, segments[0].size)
    }

    @Test
    fun `coordinates normalize hour to x and score to inverted y`() {
        val segments = CurveGeometry.segments(listOf(point(0, 100), point(23, 0)))

        val (x0, y0) = segments[0][0]
        val (x1, y1) = segments[0][1]
        assertEquals(0f, x0, 1e-6f)
        assertEquals(0f, y0, 1e-6f) // score 100 → top
        assertEquals(1f, x1, 1e-6f)
        assertEquals(1f, y1, 1e-6f) // score 0 → bottom
    }

    @Test
    fun `glance fixture yields one run covering hours 7 to 14`() {
        val briefing = GlanceBriefing.parse(Fixtures.full())

        val segments = CurveGeometry.segments(briefing.energy.curve24h)

        assertEquals(1, segments.size)
        assertEquals(8, segments[0].size) // hours 7..14 have scores
    }
}
