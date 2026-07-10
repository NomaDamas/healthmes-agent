package com.healthmes.companion.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.LocalSize
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.color.ColorProvider
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.padding
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.BriefingRepository
import com.healthmes.companion.PairingActivity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Glanceable briefing widget (issue #7): energy score, next block, and
 * unresolved-alert count from the cached `GET /v1/briefing/glance` payload.
 * Rendering never fetches — the 15-minute RefreshWorker owns the network.
 *
 * PLACEHOLDER VISUALS: layout/typography here are plumbing-grade. The final
 * glanceable grammar (what deserves the 3-second read, colors, thresholds)
 * is the healthcare domain expert's deliverable —
 * docs/design/WATCH-NOTIFICATIONS.ko.md.
 */
class BriefingWidget : GlanceAppWidget() {

    /** Two breakpoints: SMALL = score only (2x1), MEDIUM = score + lines. */
    override val sizeMode: SizeMode = SizeMode.Responsive(setOf(SMALL, MEDIUM))

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val ui = withContext(Dispatchers.IO) { loadState(context) }
        provideContent { WidgetContent(ui) }
    }

    private fun loadState(context: Context): WidgetUiState {
        val repository = BriefingRepository(context)
        if (!repository.prefs.isPaired) return WidgetUiState(paired = false, display = null)
        val display = repository.cached()?.let { BriefingDisplayState.from(it) }
        return WidgetUiState(paired = true, display = display)
    }

    companion object {
        val SMALL = androidx.compose.ui.unit.DpSize(110.dp, 40.dp)
        val MEDIUM = androidx.compose.ui.unit.DpSize(250.dp, 110.dp)
    }
}

internal data class WidgetUiState(
    val paired: Boolean,
    val display: BriefingDisplayState?,
)

// Day/night placeholder palette (brand green from the launcher mark).
private val bg = ColorProvider(day = Color(0xFFF6FBF8), night = Color(0xFF14211B))
private val fg = ColorProvider(day = Color(0xFF17241D), night = Color(0xFFE1EFE7))
private val accent = ColorProvider(day = Color(0xFF1B7F5C), night = Color(0xFF63C79E))
private val muted = ColorProvider(day = Color(0xFF5A6B62), night = Color(0xFF93A69B))
private val alert = ColorProvider(day = Color(0xFFB3261E), night = Color(0xFFF2B8B5))

@Composable
private fun WidgetContent(state: WidgetUiState) {
    val size = LocalSize.current
    Column(
        modifier = GlanceModifier
            .fillMaxSize()
            .background(bg)
            .padding(12.dp)
            // Tap-through to the pairing/status screen; decision-viewer deep
            // links stay on the notification path where the URL is fresh.
            .clickable(actionStartActivity<PairingActivity>()),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        when {
            !state.paired -> Text(
                text = androidx.glance.LocalContext.current
                    .getString(com.healthmes.companion.R.string.widget_not_paired),
                style = TextStyle(color = muted, fontSize = 13.sp),
            )

            else -> {
                ScoreRow(state.display)
                if (size.width >= BriefingWidget.MEDIUM.width) {
                    DetailLines(state.display)
                }
            }
        }
    }
}

@Composable
private fun ScoreRow(display: BriefingDisplayState?) {
    val context = androidx.glance.LocalContext.current
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            text = display?.scoreText ?: BriefingDisplayState.NO_SCORE,
            style = TextStyle(color = accent, fontSize = 32.sp, fontWeight = FontWeight.Bold),
        )
        Spacer(modifier = GlanceModifier.width(8.dp))
        Column {
            Text(
                text = context.getString(com.healthmes.companion.R.string.widget_energy_label) +
                    (display?.let { " · ${it.confidence}" } ?: ""),
                style = TextStyle(color = muted, fontSize = 11.sp),
            )
            val alerts = display?.alertCount ?: 0
            Text(
                text = if (alerts > 0) {
                    "$alerts ⚠"
                } else {
                    context.getString(com.healthmes.companion.R.string.widget_alerts_none)
                },
                style = TextStyle(
                    color = if (alerts > 0) alert else muted,
                    fontSize = 11.sp,
                    fontWeight = if (alerts > 0) FontWeight.Bold else FontWeight.Normal,
                ),
            )
        }
    }
}

@Composable
private fun DetailLines(display: BriefingDisplayState?) {
    val context = androidx.glance.LocalContext.current
    if (display == null) {
        Text(
            text = context.getString(com.healthmes.companion.R.string.widget_no_data),
            style = TextStyle(color = muted, fontSize = 12.sp),
        )
        return
    }
    display.nextBlockLine?.let { line ->
        val demand = display.nextBlockDemand?.let { " [$it]" }.orEmpty()
        Text(
            text = "▸ $line$demand",
            style = TextStyle(color = fg, fontSize = 12.sp),
            maxLines = 1,
        )
    }
    display.alertSummary?.let { summary ->
        Text(
            text = "⚠ $summary",
            style = TextStyle(color = alert, fontSize = 12.sp),
            maxLines = 2,
        )
    }
}
