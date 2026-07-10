package com.healthmes.companion.work

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkRequest
import java.util.concurrent.TimeUnit

/**
 * WorkManager wiring for the briefing refresh: one unique periodic job every
 * 15 minutes (WorkManager's floor for periodic work — same cadence issue #7
 * asks for) plus a unique one-shot "refresh now". Same shape as the
 * collector's UploadScheduling.
 */
object RefreshScheduling {

    const val PERIODIC_WORK_NAME = "healthmes-briefing-refresh"
    const val ONE_SHOT_WORK_NAME = "healthmes-briefing-refresh-now"
    const val PERIOD_MINUTES = 15L

    fun schedule(context: Context) {
        val request = PeriodicWorkRequestBuilder<RefreshWorker>(PERIOD_MINUTES, TimeUnit.MINUTES)
            .setConstraints(networkConstraints())
            .setBackoffCriteria(
                BackoffPolicy.EXPONENTIAL,
                WorkRequest.MIN_BACKOFF_MILLIS,
                TimeUnit.MILLISECONDS,
            )
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            PERIODIC_WORK_NAME,
            ExistingPeriodicWorkPolicy.UPDATE,
            request,
        )
    }

    fun cancel(context: Context) {
        val workManager = WorkManager.getInstance(context)
        workManager.cancelUniqueWork(PERIODIC_WORK_NAME)
        workManager.cancelUniqueWork(ONE_SHOT_WORK_NAME)
    }

    /** Enqueue one immediate refresh (used to verify pairing end to end). */
    fun refreshNow(context: Context) {
        val request = OneTimeWorkRequestBuilder<RefreshWorker>()
            .setConstraints(networkConstraints())
            .setBackoffCriteria(
                BackoffPolicy.EXPONENTIAL,
                WorkRequest.MIN_BACKOFF_MILLIS,
                TimeUnit.MILLISECONDS,
            )
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            ONE_SHOT_WORK_NAME,
            ExistingWorkPolicy.REPLACE,
            request,
        )
    }

    private fun networkConstraints(): Constraints =
        Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
}
