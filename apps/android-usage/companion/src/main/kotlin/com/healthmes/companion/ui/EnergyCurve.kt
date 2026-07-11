package com.healthmes.companion.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import com.healthmes.briefing.GlanceBriefing

/**
 * Pure geometry for the hand-drawn 24h energy curve: maps the glance
 * `curve_24h` (24 points, honest nulls) to normalized [0,1]×[0,1] polyline
 * segments — consecutive non-null runs only, so missing hours render as real
 * gaps instead of interpolated lies. JVM unit-tested.
 */
object CurveGeometry {

    /** x = hour/23 (0..1 left→right), y = 1 - score/100 (0..1 top→bottom). */
    fun segments(curve: List<GlanceBriefing.CurvePoint>): List<List<Pair<Float, Float>>> {
        val out = mutableListOf<List<Pair<Float, Float>>>()
        var run = mutableListOf<Pair<Float, Float>>()
        for (point in curve) {
            val score = point.score
            if (score == null) {
                if (run.isNotEmpty()) {
                    out += run
                    run = mutableListOf()
                }
            } else {
                run += Pair(point.hour / 23f, 1f - score / 100f)
            }
        }
        if (run.isNotEmpty()) out += run
        return out
    }
}

/**
 * PLACEHOLDER VISUALS: plumbing-grade sparkline (line weight, grid, colors
 * are engineering defaults — the glanceable grammar is the domain expert's
 * deliverable, docs/design/WATCH-NOTIFICATIONS.ko.md). The information
 * architecture is real: 24 local hours, gaps where data is honestly missing,
 * a marker at the current local hour.
 */
@Composable
fun EnergyCurve(
    curve: List<GlanceBriefing.CurvePoint>,
    currentHour: Int?,
    modifier: Modifier = Modifier,
) {
    val lineColor = MaterialTheme.colorScheme.primary
    val gridColor = MaterialTheme.colorScheme.surfaceVariant
    val markerColor = MaterialTheme.colorScheme.onSurfaceVariant
    val segments = CurveGeometry.segments(curve)

    Canvas(
        modifier = modifier
            .fillMaxWidth()
            .height(96.dp)
    ) {
        val w = size.width
        val h = size.height

        // Grid: score 0 / 50 / 100 baselines.
        for (fraction in listOf(0f, 0.5f, 1f)) {
            val y = h * fraction
            drawLine(
                color = gridColor,
                start = Offset(0f, y),
                end = Offset(w, y),
                strokeWidth = 1.dp.toPx(),
            )
        }

        // Current local hour marker (dashed vertical).
        currentHour?.takeIf { it in 0..23 }?.let { hour ->
            val x = w * (hour / 23f)
            drawLine(
                color = markerColor,
                start = Offset(x, 0f),
                end = Offset(x, h),
                strokeWidth = 1.dp.toPx(),
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(6f, 6f)),
            )
        }

        // The curve itself: line per run, dot for isolated hours.
        for (segment in segments) {
            if (segment.size == 1) {
                val (x, y) = segment.first()
                drawCircle(
                    color = lineColor,
                    radius = 3.dp.toPx(),
                    center = Offset(x * w, y * h),
                )
            } else {
                val path = Path()
                segment.forEachIndexed { index, (x, y) ->
                    if (index == 0) path.moveTo(x * w, y * h) else path.lineTo(x * w, y * h)
                }
                drawPath(
                    path = path,
                    color = lineColor,
                    style = Stroke(width = 2.dp.toPx()),
                )
            }
        }
    }
}
