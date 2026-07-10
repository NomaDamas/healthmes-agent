package com.healthmes.companion.work

import android.content.Context
import androidx.glance.appwidget.updateAll
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.healthmes.briefing.BriefingRepository
import com.healthmes.briefing.GlanceBriefing
import com.healthmes.companion.notify.AlertNotifier
import com.healthmes.companion.notify.NotificationGrammar
import com.healthmes.companion.widget.BriefingWidget
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * The 15-minute refresh: conditional GET of `/v1/briefing/glance` with the
 * cached ETag (`If-None-Match`; 304 keeps the cached payload — the endpoint's
 * documented client behavior), then re-render every widget and run the local
 * alert heuristic.
 *
 * PLACEHOLDER ALERT LOGIC: a notification fires when `alerts.unresolved_count`
 * rises between two polls. Real proactive push (server → device, with real
 * action wiring) is future work; this local trigger only proves the §8.5
 * notification grammar rendering end to end.
 */
class RefreshWorker(appContext: Context, params: WorkerParameters) :
    CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val context = applicationContext
        val repository = BriefingRepository(context)
        val prefs = repository.prefs

        when (val outcome = repository.refresh()) {
            is BriefingRepository.RefreshOutcome.NotPaired -> {
                prefs.lastResult = "Not paired: save the server URL first."
                Result.success()
            }

            is BriefingRepository.RefreshOutcome.Updated -> {
                prefs.lastResult = "Updated (${stamp()})."
                maybeNotifyRisingAlerts(context, outcome.briefing)
                BriefingWidget().updateAll(context)
                Result.success()
            }

            is BriefingRepository.RefreshOutcome.Unchanged -> {
                prefs.lastResult = "Up to date — 304 (${stamp()})."
                BriefingWidget().updateAll(context)
                Result.success()
            }

            is BriefingRepository.RefreshOutcome.Failed -> {
                prefs.lastResult =
                    "Refresh failed${if (outcome.transient) ", will retry" else ""}: " +
                        "${outcome.reason} (${stamp()})"
                BriefingWidget().updateAll(context)
                if (outcome.transient) Result.retry() else Result.failure()
            }
        }
    }

    /**
     * Rising-count heuristic: the first successful fetch only establishes the
     * baseline (never notifies — PLAN.md §11 treats alert noise as the top
     * product risk), later rises raise one grammar-shaped notification.
     */
    private fun maybeNotifyRisingAlerts(context: Context, briefing: GlanceBriefing) {
        val prefs = BriefingRepository(context).prefs
        val lastSeen = prefs.lastSeenAlertCount
        val current = briefing.alerts.unresolvedCount
        if (lastSeen in 0 until current) {
            NotificationGrammar.fromGlance(briefing)?.let { grammar ->
                AlertNotifier.notify(context, grammar)
            }
        }
        prefs.lastSeenAlertCount = current
    }

    private fun stamp(): String =
        TIME_FORMAT.withZone(ZoneId.systemDefault()).format(Instant.now())

    private companion object {
        val TIME_FORMAT: DateTimeFormatter = DateTimeFormatter.ofPattern("MMM d HH:mm")
    }
}
