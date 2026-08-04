package com.healthmes.companion.work

import android.content.Context
import androidx.glance.appwidget.updateAll
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.healthmes.api.AlertsPage
import com.healthmes.api.HealthmesApi
import com.healthmes.briefing.BriefingRepository
import com.healthmes.briefing.GlanceBriefing
import com.healthmes.briefing.PairingPrefs
import com.healthmes.companion.notify.AlertNotifier
import com.healthmes.companion.notify.FocusBlockNotifier
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
 * documented client behavior), then re-render every widget, keep the ongoing
 * focus-block notification honest, and run the local alert heuristic.
 *
 * PLACEHOLDER ALERT *TRIGGER*: a notification fires when
 * `alerts.unresolved_count` rises between two polls (no push relay by design
 * — Telegram stays the guaranteed channel; docs/PLAN.md §11 treats alert
 * noise as the top product risk, so the first fetch only sets the baseline).
 * The notification *content* is real (issue #10): the newest `GET /v1/alerts`
 * item carries the §8.5 lines the trigger recorded at fire time, and the
 * buttons drive the schedule-proposal endpoints.
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
                FocusBlockNotifier.update(context, briefing = null)
                Result.success()
            }

            is BriefingRepository.RefreshOutcome.Updated -> {
                prefs.lastResult = "Updated (${stamp()})."
                maybeNotifyRisingAlerts(context, prefs, outcome.briefing)
                FocusBlockNotifier.update(context, outcome.briefing)
                BriefingWidget().updateAll(context)
                Result.success()
            }

            is BriefingRepository.RefreshOutcome.Unchanged -> {
                prefs.lastResult = "Up to date — 304 (${stamp()})."
                outcome.briefing?.let { maybeNotifyRisingAlerts(context, prefs, it) }
                FocusBlockNotifier.update(context, outcome.briefing)
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
     * baseline (never notifies — PLAN.md §11), later rises raise one
     * grammar-shaped notification, preferring the real fire-time lines from
     * `GET /v1/alerts` over the glance-derived fallback.
     */
    private fun maybeNotifyRisingAlerts(
        context: Context,
        prefs: PairingPrefs,
        briefing: GlanceBriefing,
    ) {
        val lastSeen = prefs.lastSeenAlertCount
        val current = briefing.alerts.unresolvedCount
        val fetchedAlerts = fetchAlertGrammars(prefs)
        val alerts = fetchedAlerts.orEmpty()
        val newest = alerts.firstOrNull()
        val fallback = NotificationGrammar.fromGlance(briefing)
        if (!prefs.alertNotificationStateInitialized) {
            if (fetchedAlerts == null) {
                if (lastSeen in 0 until current) {
                    fallback?.let {
                        AlertNotifier.notify(context, it, proposalId = it.proposalId)
                        it.alertRevision?.let { revision ->
                            prefs.alertNotificationFallbackRevisions =
                                prefs.alertNotificationFallbackRevisions + revision
                        }
                    }
                }
            } else {
                if (lastSeen < 0) {
                    prefs.alertNotificationRevisions =
                        NotificationGrammar.currentRevisions(alerts)
                } else {
                    val previous = NotificationGrammar.legacyRevisions(alerts)
                    val grammar =
                        if (lastSeen < current) newest ?: fallback
                        else NotificationGrammar.pendingNotification(previous, alerts)
                    grammar?.let {
                        AlertNotifier.notify(context, it, proposalId = it.proposalId)
                    }
                    prefs.alertNotificationRevisions =
                        NotificationGrammar.mergeRevisions(
                            previous,
                            alerts,
                            grammar?.alertRevision,
                        )
                }
                prefs.alertNotificationStateInitialized = true
                prefs.alertNotificationFallbackRevisions = emptySet()
            }
        } else {
            val countRose = current > lastSeen
            val previous =
                prefs.alertNotificationRevisions + prefs.alertNotificationFallbackRevisions
            val grammar =
                fetchedAlerts?.let {
                    NotificationGrammar.pendingNotification(previous, alerts)
                        ?: if (countRose) fallback else null
                } ?: if (countRose) fallback else null
            grammar?.let {
                AlertNotifier.notify(context, it, proposalId = it.proposalId)
            }
            if (fetchedAlerts != null) {
                prefs.alertNotificationRevisions =
                    NotificationGrammar.mergeRevisions(
                        previous,
                        alerts,
                        grammar?.alertRevision,
                    )
                prefs.alertNotificationFallbackRevisions = emptySet()
            } else if (countRose) {
                fallback?.let {
                    it.alertRevision?.let { revision ->
                        prefs.alertNotificationFallbackRevisions =
                            prefs.alertNotificationFallbackRevisions + revision
                    }
                }
            }
        }
        if (
            prefs.alertNotificationStateInitialized ||
            fetchedAlerts != null ||
            lastSeen >= 0
        ) {
            prefs.lastSeenAlertCount = current
        }
    }

    /** Recent pushed alerts with their real §8.5 lines; null on any failure. */
    private fun fetchAlertGrammars(prefs: PairingPrefs): List<NotificationGrammar>? {
        val serverUrl = prefs.serverUrl ?: return null
        val response = HealthmesApi(serverUrl, prefs.token)
            .get("${AlertsPage.ENDPOINT_PATH}?limit=50&offset=0")
        if (response !is HealthmesApi.Response.Http || !response.isSuccess) return null
        return runCatching { AlertsPage.parse(response.body) }
            .getOrNull()
            ?.alerts
            ?.map { NotificationGrammar.fromAlert(it) }
    }

    private fun stamp(): String =
        TIME_FORMAT.withZone(ZoneId.systemDefault()).format(Instant.now())

    private companion object {
        val TIME_FORMAT: DateTimeFormatter = DateTimeFormatter.ofPattern("MMM d HH:mm")
    }
}
