package com.healthmes.wear.tile

import androidx.concurrent.futures.CallbackToFutureAdapter
import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.ColorBuilders.argb
import androidx.wear.protolayout.DeviceParametersBuilders.DeviceParameters
import androidx.wear.protolayout.DimensionBuilders.expand
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.protolayout.material.Text
import androidx.wear.protolayout.material.Typography
import androidx.wear.protolayout.material.layouts.PrimaryLayout
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.ListenableFuture
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.BriefingRepository
import com.healthmes.wear.R
import com.healthmes.wear.WearPairingActivity
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Wear OS tile: energy score + next block + alert count from the paired
 * HealthMes instance's `GET /v1/briefing/glance`. Serving is cache-first
 * (mirroring the endpoint's max-age=300); a fetch happens on the tile's
 * background executor only when the cache is stale, so a swipe to the tile
 * never blocks on the LAN.
 *
 * Tapping anywhere on the tile opens the on-watch briefing view (issue #7
 * acceptance: a glance surface tap opens the briefing) — currently
 * [WearPairingActivity], whose status readout doubles as the placeholder
 * briefing screen.
 *
 * PLACEHOLDER VISUALS: this layout only proves the plumbing. What the watch
 * should actually say at a glance (thresholds, wording, color semantics,
 * haptics) is the healthcare domain expert's deliverable —
 * docs/design/WATCH-NOTIFICATIONS.ko.md.
 */
class BriefingTileService : TileService() {

    private lateinit var executor: ExecutorService

    override fun onCreate() {
        super.onCreate()
        executor = Executors.newSingleThreadExecutor()
    }

    override fun onDestroy() {
        executor.shutdown()
        super.onDestroy()
    }

    override fun onTileRequest(
        requestParams: RequestBuilders.TileRequest,
    ): ListenableFuture<TileBuilders.Tile> =
        CallbackToFutureAdapter.getFuture { completer ->
            executor.execute {
                try {
                    completer.set(buildTile(requestParams.deviceConfiguration))
                } catch (t: Throwable) {
                    completer.setException(t)
                }
            }
            "BriefingTileService.onTileRequest"
        }

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest,
    ): ListenableFuture<ResourceBuilders.Resources> =
        CallbackToFutureAdapter.getFuture { completer ->
            completer.set(
                ResourceBuilders.Resources.Builder().setVersion(RESOURCES_VERSION).build()
            )
            "BriefingTileService.onTileResourcesRequest"
        }

    private fun buildTile(deviceParams: DeviceParameters): TileBuilders.Tile {
        val repository = BriefingRepository(this)
        val state = when {
            !repository.prefs.isPaired -> null
            else -> repository.freshOrCached()?.let { BriefingDisplayState.from(it) }
        }
        return TileBuilders.Tile.Builder()
            .setResourcesVersion(RESOURCES_VERSION)
            // Ask the renderer to re-request after the widget cadence.
            .setFreshnessIntervalMillis(FRESHNESS_MS)
            .setTileTimeline(
                TimelineBuilders.Timeline.fromLayoutElement(
                    layout(state, paired = repository.prefs.isPaired, deviceParams)
                )
            )
            .build()
    }

    private fun layout(
        state: BriefingDisplayState?,
        paired: Boolean,
        deviceParams: DeviceParameters,
    ): LayoutElementBuilders.LayoutElement {
        val column = LayoutElementBuilders.Column.Builder()
            .addContent(
                Text.Builder(this, state?.scoreText ?: BriefingDisplayState.NO_SCORE)
                    .setTypography(Typography.TYPOGRAPHY_DISPLAY1)
                    .setColor(argb(COLOR_ACCENT))
                    .build()
            )
            .addContent(
                Text.Builder(
                    this,
                    getString(R.string.tile_energy_label) +
                        (state?.let { " · ${it.confidence}" }.orEmpty()),
                )
                    .setTypography(Typography.TYPOGRAPHY_CAPTION1)
                    .setColor(argb(COLOR_MUTED))
                    .build()
            )
            .addContent(
                Text.Builder(this, secondaryLine(state, paired))
                    .setTypography(Typography.TYPOGRAPHY_CAPTION2)
                    .setColor(argb(COLOR_FOREGROUND))
                    .setMaxLines(2)
                    .build()
            )
            .addContent(
                Text.Builder(this, alertLine(state))
                    .setTypography(Typography.TYPOGRAPHY_CAPTION2)
                    .setColor(argb(if ((state?.alertCount ?: 0) > 0) COLOR_ALERT else COLOR_MUTED))
                    .setMaxLines(1)
                    .build()
            )
            .build()

        // Whole-surface tap → on-watch briefing view (placeholder:
        // pairing/status screen). Wrapping the PrimaryLayout in an expanded
        // clickable Box keeps every pixel of the tile tappable.
        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(expand())
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setClickable(openBriefingClickable())
                    .build()
            )
            .addContent(
                PrimaryLayout.Builder(deviceParams)
                    .setResponsiveContentInsetEnabled(true)
                    .setContent(column)
                    .build()
            )
            .build()
    }

    private fun openBriefingClickable(): ModifiersBuilders.Clickable =
        ModifiersBuilders.Clickable.Builder()
            .setId(CLICKABLE_OPEN_BRIEFING)
            .setOnClick(
                ActionBuilders.LaunchAction.Builder()
                    .setAndroidActivity(
                        ActionBuilders.AndroidActivity.Builder()
                            .setPackageName(packageName)
                            .setClassName(WearPairingActivity::class.java.name)
                            .build()
                    )
                    .build()
            )
            .build()

    private fun secondaryLine(state: BriefingDisplayState?, paired: Boolean): String = when {
        !paired -> getString(R.string.tile_not_paired)
        state?.nextBlockLine != null -> "▸ ${state.nextBlockLine}"
        else -> getString(R.string.tile_no_blocks)
    }

    private fun alertLine(state: BriefingDisplayState?): String {
        val count = state?.alertCount ?: 0
        return if (count > 0) "⚠ $count unresolved" else getString(R.string.tile_alerts_none)
    }

    private companion object {
        const val RESOURCES_VERSION = "1"
        const val FRESHNESS_MS = 15L * 60L * 1000L
        const val CLICKABLE_OPEN_BRIEFING = "open_briefing"

        // Placeholder palette matching the phone widget (dark tile canvas).
        const val COLOR_ACCENT = 0xFF63C79E.toInt()
        const val COLOR_FOREGROUND = 0xFFE1EFE7.toInt()
        const val COLOR_MUTED = 0xFF93A69B.toInt()
        const val COLOR_ALERT = 0xFFF2B8B5.toInt()
    }
}
