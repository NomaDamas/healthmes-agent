package com.healthmes.companion.widget

import android.content.Context
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import com.healthmes.briefing.PairingPrefs
import com.healthmes.companion.work.RefreshScheduling

/** Hosts [BriefingWidget]; adding the first widget kicks the refresh loop. */
class BriefingWidgetReceiver : GlanceAppWidgetReceiver() {

    override val glanceAppWidget: GlanceAppWidget = BriefingWidget()

    override fun onEnabled(context: Context) {
        super.onEnabled(context)
        if (PairingPrefs(context).isPaired) {
            RefreshScheduling.schedule(context)
            RefreshScheduling.refreshNow(context)
        }
    }

    // onDisabled intentionally does NOT cancel the periodic refresh: the
    // alert notification channel keeps working without any widget placed.
    // "Clear pairing" in PairingActivity is the explicit off switch.
}
