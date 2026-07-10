package com.healthmes.wear.complication

import android.app.PendingIntent
import android.content.Intent
import androidx.wear.watchface.complications.data.ComplicationData
import androidx.wear.watchface.complications.data.ComplicationType
import androidx.wear.watchface.complications.data.NoDataComplicationData
import androidx.wear.watchface.complications.data.PlainComplicationText
import androidx.wear.watchface.complications.data.RangedValueComplicationData
import androidx.wear.watchface.complications.data.ShortTextComplicationData
import androidx.wear.watchface.complications.datasource.ComplicationDataSourceService
import androidx.wear.watchface.complications.datasource.ComplicationRequest
import com.healthmes.briefing.BriefingRepository
import com.healthmes.wear.WearPairingActivity
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Watch-face complication exposing the cognitive-energy score as SHORT_TEXT
 * ("72" with an "NRG" title) and RANGED_VALUE (0-100 gauge). Cache-first like
 * the tile: the network is touched only when the cached briefing is older
 * than the endpoint's max-age.
 *
 * Tapping the complication opens the on-watch briefing view (issue #7
 * acceptance: a glance surface tap opens the briefing) — currently
 * [WearPairingActivity], whose status readout doubles as the placeholder
 * briefing screen.
 *
 * PLACEHOLDER VISUALS: text/labels here are plumbing-grade; the final
 * complication semantics belong to the healthcare domain expert
 * (docs/design/WATCH-NOTIFICATIONS.ko.md).
 */
class EnergyComplicationService : ComplicationDataSourceService() {

    private lateinit var executor: ExecutorService

    override fun onCreate() {
        super.onCreate()
        executor = Executors.newSingleThreadExecutor()
    }

    override fun onDestroy() {
        executor.shutdown()
        super.onDestroy()
    }

    override fun onComplicationRequest(
        request: ComplicationRequest,
        listener: ComplicationRequestListener,
    ) {
        // The listener contract allows an async reply; the fetch (if any)
        // happens off the binder thread.
        executor.execute {
            val score = BriefingRepository(this).freshOrCached()?.energy?.score
            listener.onComplicationData(buildData(request.complicationType, score))
        }
    }

    override fun getPreviewData(type: ComplicationType): ComplicationData? =
        buildData(type, PREVIEW_SCORE)

    private fun buildData(type: ComplicationType, score: Int?): ComplicationData? = when (type) {
        ComplicationType.SHORT_TEXT ->
            if (score == null) {
                NoDataComplicationData()
            } else {
                ShortTextComplicationData.Builder(
                    PlainComplicationText.Builder(score.toString()).build(),
                    contentDescription(score),
                )
                    .setTitle(PlainComplicationText.Builder(TITLE).build())
                    .setTapAction(openBriefingIntent())
                    .build()
            }

        ComplicationType.RANGED_VALUE ->
            if (score == null) {
                NoDataComplicationData()
            } else {
                RangedValueComplicationData.Builder(
                    score.toFloat(),
                    RANGE_MIN,
                    RANGE_MAX,
                    contentDescription(score),
                )
                    .setText(PlainComplicationText.Builder(score.toString()).build())
                    .setTitle(PlainComplicationText.Builder(TITLE).build())
                    .setTapAction(openBriefingIntent())
                    .build()
            }

        else -> null // Only SHORT_TEXT and RANGED_VALUE are declared supported.
    }

    private fun contentDescription(score: Int) =
        PlainComplicationText.Builder("Cognitive energy $score of 100").build()

    /** Tap-through to the on-watch briefing view (placeholder: pairing/status screen). */
    private fun openBriefingIntent(): PendingIntent =
        PendingIntent.getActivity(
            this,
            TAP_REQUEST_CODE,
            Intent(this, WearPairingActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    private companion object {
        const val TITLE = "NRG"
        const val PREVIEW_SCORE = 72
        const val RANGE_MIN = 0f
        const val RANGE_MAX = 100f
        const val TAP_REQUEST_CODE = 0
    }
}
